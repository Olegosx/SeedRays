"""The Operator API route group (ADR-0004, ADR-0005).

A thin adapter over the operator orchestrator operations. The panel
session rides in its own HttpOnly cookie, separate from the user
cabinet; mutating requests carry the X-CSRF-Token header.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.api.errors import ApiError
from seedrays.orchestrator import billing as billing_ops
from seedrays.orchestrator import operator as operator_ops
from seedrays.orchestrator.captcha import CaptchaGuard
from seedrays.orchestrator.operations import OperationError
from seedrays.orchestrator.ratelimit import RateLimiter
from seedrays.orchestrator.seclog import ACTOR_OPERATOR, OUTCOME_SUCCESS, SecurityLog
from seedrays.storage import billing as billing_store
from seedrays.storage.engine import (
	billing_db_path,
	create_sqlite_engine,
	now_utc,
	registry_db_path,
)

OPERATOR_COOKIE = "seedrays_operator"

LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60


class OperatorLoginRequest(BaseModel):
	"""Body of the operator sign-in."""

	login: str = Field(min_length=1, max_length=64)
	password: str = Field(min_length=1, max_length=1024)
	captcha: str = Field(min_length=1, max_length=4096)


class OperatorPasswordRequest(BaseModel):
	"""Body of the operator password change."""

	current_password: str = Field(min_length=1, max_length=1024)
	new_password: str = Field(min_length=1, max_length=1024)


class UserStatusRequest(BaseModel):
	"""Body of the user block/unblock operation."""

	status: str = Field(min_length=1, max_length=16)


class UserDeleteRequest(BaseModel):
	"""Body of the user deletion: the login retyped by the operator."""

	username: str = Field(min_length=1, max_length=64)


class SettingsRequest(BaseModel):
	"""Body of the settings update: key → value."""

	values: dict[str, str]


class MasterWalletRequest(BaseModel):
	"""Body of the master-wallet operation (ADR-0027)."""

	network: str = Field(min_length=1, max_length=32)
	xpub: str = Field(min_length=1, max_length=256)


class ConfirmPaymentRequest(BaseModel):
	"""Body of the manual payment confirmation: why it is settled by hand."""

	reason: str = Field(min_length=1, max_length=500)


@dataclass
class OperatorContext:
	"""Per-request registry engine and the resolved operator session."""

	registry: AsyncEngine
	operator: operator_ops.CurrentOperator
	session_token: str


def register_operator_routes(
	app: FastAPI,
	data_dir: Path,
	captcha_cost: int | None = None,
	seclog: SecurityLog | None = None,
) -> None:
	"""Attach the /v1/operator route group.

	Args:
		app: The FastAPI application.
		data_dir: The gateway data directory.
		captcha_cost: Proof-of-work cost override (tests); by default
			the production cost of the captcha module.
		seclog: The gateway's security journal; created here when the
			caller did not pass the shared instance.
	"""
	login_limiter = RateLimiter(LOGIN_LIMIT, LOGIN_WINDOW_SECONDS)
	captcha_guard = CaptchaGuard(cost=captcha_cost) if captcha_cost else CaptchaGuard()
	journal = seclog if seclog is not None else SecurityLog(data_dir)

	async def registry_engine() -> AsyncIterator[AsyncEngine]:
		"""Open the registry engine for one request."""
		engine = create_sqlite_engine(registry_db_path(data_dir))
		try:
			yield engine
		finally:
			await engine.dispose()

	RegistryDep = Depends(registry_engine)

	async def billing_engine() -> AsyncIterator[AsyncEngine]:
		"""Open the billing database for one request."""
		engine = create_sqlite_engine(billing_db_path(data_dir))
		try:
			yield engine
		finally:
			await engine.dispose()

	BillingDep = Depends(billing_engine)

	async def operator_session(
		request: Request, registry: AsyncEngine = RegistryDep
	) -> OperatorContext:
		"""Authenticate the operator cookie."""
		token = request.cookies.get(OPERATOR_COOKIE)
		if not token:
			raise ApiError(401, "unauthorized", "sign in first")
		operator = await operator_ops.resolve_session(registry, token)
		if operator is None:
			raise ApiError(401, "unauthorized", "the session is expired or unknown")
		return OperatorContext(registry=registry, operator=operator, session_token=token)

	SessionDep = Depends(operator_session)

	async def mutating_session(
		request: Request, ctx: OperatorContext = SessionDep
	) -> OperatorContext:
		"""Session + CSRF in one structural dependency (as in the user group)."""
		header_token = request.headers.get("X-CSRF-Token")
		if not header_token or not hmac.compare_digest(
			header_token, ctx.operator.csrf_token
		):
			raise ApiError(403, "csrf", "the X-CSRF-Token header is missing or wrong")
		return ctx

	MutatingSessionDep = Depends(mutating_session)

	@app.get("/v1/operator/captcha")
	async def issue_captcha() -> dict:
		"""One signed proof-of-work challenge for the ALTCHA widget."""
		return captcha_guard.issue()

	@app.post("/v1/operator/login")
	async def login(
		body: OperatorLoginRequest,
		request: Request,
		response: Response,
		registry: AsyncEngine = RegistryDep,
	) -> dict:
		"""Operator sign-in; sets the panel session cookie."""
		client = request.client.host if request.client else "unknown"
		if not login_limiter.allow(f"{client}|{body.login.strip().lower()}"):
			await journal.event(
				registry, "login", actor=ACTOR_OPERATOR, outcome="rate_limited",
				identifier=body.login.strip(), client=client,
			)
			raise ApiError(429, "rate_limited", "too many sign-in attempts; try again later")
		if not captcha_guard.verify(body.captcha):
			await journal.event(
				registry, "login", actor=ACTOR_OPERATOR, outcome="captcha_failed",
				identifier=body.login.strip(), client=client,
			)
			raise ApiError(
				400, "captcha_failed", "the proof-of-work check failed; please try again"
			)
		signed = await operator_ops.sign_in(
			registry, login=body.login, password=body.password,
			client=client, seclog=journal,
		)
		response.set_cookie(
			OPERATOR_COOKIE,
			signed.session_token,
			max_age=int((signed.expires_at - now_utc()).total_seconds()),
			httponly=True,
			secure=True,
			samesite="lax",
			path="/",
		)
		return {"operator": {"login": signed.login}, "csrf": signed.csrf_token}

	@app.post("/v1/operator/logout")
	async def logout(
		response: Response, ctx: OperatorContext = MutatingSessionDep
	) -> dict:
		"""Drop the panel session and clear the cookie."""
		await operator_ops.sign_out(ctx.registry, ctx.session_token)
		response.delete_cookie(OPERATOR_COOKIE, path="/")
		return {"ok": True}

	@app.get("/v1/operator/me")
	async def me(ctx: OperatorContext = SessionDep) -> dict:
		"""The signed-in operator and the CSRF token of this session."""
		return {
			"operator": {"login": ctx.operator.login},
			"csrf": ctx.operator.csrf_token,
		}

	@app.post("/v1/operator/password")
	async def change_password(
		body: OperatorPasswordRequest,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
	) -> dict:
		"""Change the operator's password; other panel sessions are dropped."""
		client = request.client.host if request.client else "unknown"
		try:
			await operator_ops.change_password(
				ctx.registry,
				operator_id=ctx.operator.operator_id,
				current_password=body.current_password,
				new_password=body.new_password,
				session_token=ctx.session_token,
			)
		except OperationError as exc:
			await journal.event(
				ctx.registry, "password_change", actor=ACTOR_OPERATOR,
				outcome=exc.code, operator_id=ctx.operator.operator_id, client=client,
			)
			raise
		await journal.event(
			ctx.registry, "password_change", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id, client=client,
		)
		return {"ok": True}

	@app.get("/v1/operator/users")
	async def list_users(ctx: OperatorContext = SessionDep) -> dict:
		"""Every gateway user with emails, status and wallet counts."""
		users = await operator_ops.list_gateway_users(ctx.registry, data_dir)
		return {
			"users": [
				{
					"id": u.id,
					"username": u.username,
					"status": u.status,
					"emails": u.emails,
					"wallets": u.wallets,
					"created_at": u.created_at,
				}
				for u in users
			]
		}

	@app.post("/v1/operator/users/{user_id}/status")
	async def set_user_status(
		user_id: int,
		body: UserStatusRequest,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
	) -> dict:
		"""Block or unblock a gateway user."""
		await operator_ops.set_user_status(
			ctx.registry, user_id=user_id, status=body.status
		)
		# Административное действие над чужой учёткой — событие журнала.
		await journal.event(
			ctx.registry, "user_status", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"target_user_id": user_id, "status": body.status},
		)
		return {"ok": True}

	@app.post("/v1/operator/users/{user_id}/password-reset")
	async def reset_user_password(
		user_id: int, request: Request, ctx: OperatorContext = MutatingSessionDep
	) -> dict:
		"""Set a random temporary password for a user; it is returned once."""
		password = await operator_ops.reset_user_password(ctx.registry, user_id=user_id)
		await journal.event(
			ctx.registry, "user_password_reset", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"target_user_id": user_id},
		)
		return {"password": password}

	@app.post("/v1/operator/users/{user_id}/delete")
	async def delete_user(
		user_id: int,
		body: UserDeleteRequest,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
	) -> dict:
		"""Archive and delete a blocked user; the retyped login must match."""
		archive = await operator_ops.delete_user(
			ctx.registry, data_dir, user_id=user_id, username=body.username
		)
		await journal.event(
			ctx.registry, "user_delete", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"target_user_id": user_id, "username": body.username, "archive": archive},
		)
		return {"ok": True}

	@app.get("/v1/operator/settings")
	async def get_settings(ctx: OperatorContext = SessionDep) -> dict:
		"""The panel's settings; secret values are reported only as set/unset."""
		return {"settings": await operator_ops.get_settings(ctx.registry)}

	@app.put("/v1/operator/settings")
	async def update_settings(
		body: SettingsRequest, request: Request, ctx: OperatorContext = MutatingSessionDep
	) -> dict:
		"""Store the submitted settings (an empty secret means "keep")."""
		await operator_ops.update_settings(ctx.registry, body.values)
		# В журнал — только КЛЮЧИ изменённых настроек, никогда не значения.
		await journal.event(
			ctx.registry, "settings_update", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"keys": sorted(body.values)},
		)
		return {"settings": await operator_ops.get_settings(ctx.registry)}

	@app.get("/v1/operator/billing/wallets")
	async def list_master_wallets(
		ctx: OperatorContext = SessionDep, billing: AsyncEngine = BillingDep
	) -> dict:
		"""The owner's master wallets by payment network (ADR-0027)."""
		wallets = await billing_store.list_master_wallets(billing)
		return {
			"wallets": [{"network": w.network, "xpub": w.xpub} for w in wallets],
			"networks": sorted(billing_ops.supported_payment_networks()),
		}

	@app.put("/v1/operator/billing/wallets")
	async def set_master_wallet(
		body: MasterWalletRequest,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
		billing: AsyncEngine = BillingDep,
	) -> dict:
		"""Set the master wallet of one payment network; the xpub must be free."""
		await billing_ops.attach_master_wallet(
			billing, ctx.registry, network=body.network, xpub=body.xpub
		)
		await journal.event(
			ctx.registry, "billing_master_wallet", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"network": body.network},
		)
		return {"ok": True}

	@app.delete("/v1/operator/billing/wallets/{network}")
	async def delete_master_wallet(
		network: str,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
		billing: AsyncEngine = BillingDep,
	) -> dict:
		"""Drop the master wallet of one network; issued invoices keep their addresses."""
		await billing_store.delete_master_wallet(billing, network)
		await journal.event(
			ctx.registry, "billing_master_wallet_removed", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"network": network},
		)
		return {"ok": True}

	@app.get("/v1/operator/billing/invoices")
	async def list_invoices(
		ctx: OperatorContext = SessionDep,
		billing: AsyncEngine = BillingDep,
		state: str | None = None,
	) -> dict:
		"""Every user's invoices, newest period first; filterable by state."""
		invoices = await billing_ops.panel_invoices(billing, ctx.registry, state=state)
		return {"invoices": [vars(invoice) for invoice in invoices]}

	@app.post("/v1/operator/billing/invoices/{invoice_id}/confirm")
	async def confirm_invoice(
		invoice_id: int,
		body: ConfirmPaymentRequest,
		request: Request,
		ctx: OperatorContext = MutatingSessionDep,
		billing: AsyncEngine = BillingDep,
	) -> dict:
		"""Settle an invoice by hand: money that arrived outside the gateway."""
		user_id = await billing_ops.confirm_manually(
			billing,
			ctx.registry,
			journal,
			invoice_id=invoice_id,
			operator_id=ctx.operator.operator_id,
			reason=body.reason,
		)
		await journal.event(
			ctx.registry, "billing_manual_payment", actor=ACTOR_OPERATOR,
			outcome=OUTCOME_SUCCESS, operator_id=ctx.operator.operator_id,
			client=request.client.host if request.client else "unknown",
			detail={"invoice_id": invoice_id, "target_user_id": user_id, "reason": body.reason},
		)
		return {"ok": True}

	@app.get("/v1/operator/watcher")
	async def watcher(ctx: OperatorContext = SessionDep) -> dict:
		"""Per-network watcher cursors (read-only)."""
		return {"networks": await operator_ops.watcher_status(ctx.registry)}
