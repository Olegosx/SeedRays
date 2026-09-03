"""The User API route group: cabinet authentication (ADR-0004, ADR-0005).

A thin adapter over the orchestrator auth operations. The session rides
in an HttpOnly cookie; mutating requests must carry the X-CSRF-Token
header matching the session's CSRF token.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.api.errors import ApiError
from seedrays.mail.base import MailSender
from seedrays.mail.resend import ResendSender
from seedrays.orchestrator import auth
from seedrays.orchestrator import wallets as wallet_ops
from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path

SESSION_COOKIE = "seedrays_session"

# Ключи настроек почты (реестр, ADR-0016).
SETTING_MAIL_API_KEY = "mail.resend.api_key"
SETTING_MAIL_FROM = "mail.from"
SETTING_BASE_URL = "gateway.base_url"


class RegisterRequest(BaseModel):
	"""Body of the registration operation."""

	username: str = Field(min_length=1, max_length=128)
	email: str = Field(min_length=3, max_length=255)
	password: str = Field(min_length=1, max_length=1024)


class LoginRequest(BaseModel):
	"""Body of the sign-in operation."""

	identifier: str = Field(min_length=1, max_length=255)
	password: str = Field(min_length=1, max_length=1024)
	remember: bool = False


class AttachWalletRequest(BaseModel):
	"""Body of the attach-wallet operation."""

	family: str = Field(min_length=1, max_length=16)
	xpub: str = Field(min_length=1, max_length=256)
	label: str = Field(default="", max_length=64)


class GenerateWalletRequest(BaseModel):
	"""Body of the in-gateway generation operation."""

	words: int
	families: list[str] = Field(min_length=1, max_length=8)
	passphrase: str = Field(default="", max_length=256)


@dataclass
class UserContext:
	"""Per-request registry engine and the resolved session user."""

	registry: AsyncEngine
	user: auth.CurrentUser
	session_token: str


async def _resolve_mailer(registry: AsyncEngine) -> MailSender | None:
	"""Build the configured mail sender, or None when mail is not set up."""
	api_key = await registry_ops.get_setting(registry, SETTING_MAIL_API_KEY)
	from_address = await registry_ops.get_setting(registry, SETTING_MAIL_FROM)
	if not api_key or not from_address:
		return None
	return ResendSender(api_key, from_address)


def register_user_routes(
	app: FastAPI, data_dir: Path, mailer: MailSender | None = None
) -> None:
	"""Attach the /v1/user route group.

	Args:
		app: The FastAPI application.
		data_dir: The gateway data directory.
		mailer: Mail sender override (tests); by default the sender is
			built from the registry settings on every registration.
	"""

	async def registry_engine() -> AsyncIterator[AsyncEngine]:
		"""Open the registry engine for one request."""
		engine = create_sqlite_engine(registry_db_path(data_dir))
		try:
			yield engine
		finally:
			await engine.dispose()

	RegistryDep = Depends(registry_engine)

	async def session_user(
		request: Request, registry: AsyncEngine = RegistryDep
	) -> UserContext:
		"""Authenticate the session cookie."""
		token = request.cookies.get(SESSION_COOKIE)
		if not token:
			raise ApiError(401, "unauthorized", "sign in first")
		user = await auth.resolve_session(registry, token)
		if user is None:
			raise ApiError(401, "unauthorized", "the session is expired or unknown")
		return UserContext(registry=registry, user=user, session_token=token)

	SessionDep = Depends(session_user)

	def check_csrf(ctx: UserContext, header_token: str | None) -> None:
		"""Mutating requests must present the session's CSRF token."""
		if not header_token or header_token != ctx.user.csrf_token:
			raise ApiError(403, "csrf", "the X-CSRF-Token header is missing or wrong")

	def _set_session_cookie(response: Response, signed: auth.SignedIn) -> None:
		max_age = int((signed.expires_at - auth._now()).total_seconds())
		response.set_cookie(
			SESSION_COOKIE,
			signed.session_token,
			max_age=max_age,
			httponly=True,
			samesite="lax",
			path="/",
		)

	@app.post("/v1/user/register")
	async def register(
		body: RegisterRequest, request: Request, registry: AsyncEngine = RegistryDep
	) -> dict:
		"""Create an account; sends the confirmation email when mail is set up."""
		active_mailer = mailer if mailer is not None else await _resolve_mailer(registry)
		base_url = await registry_ops.get_setting(registry, SETTING_BASE_URL)
		if not base_url:
			base_url = str(request.base_url)
		registered = await auth.register(
			registry,
			data_dir,
			username=body.username,
			email=body.email,
			password=body.password,
			mailer=active_mailer,
			confirm_base_url=base_url,
		)
		return {
			"user": {"username": registered.username},
			"confirmation_required": registered.confirmation_required,
		}

	@app.get("/v1/user/confirm-email")
	async def confirm_email(token: str, registry: AsyncEngine = RegistryDep):
		"""Landing point of the link from the confirmation email."""
		confirmed = await auth.confirm_email(registry, token)
		flag = "1" if confirmed else "0"
		return RedirectResponse(f"/login.html?confirmed={flag}", status_code=303)

	@app.post("/v1/user/login")
	async def login(
		body: LoginRequest, response: Response, registry: AsyncEngine = RegistryDep
	) -> dict:
		"""Sign in by username or email; sets the session cookie."""
		signed = await auth.sign_in(
			registry,
			identifier=body.identifier,
			password=body.password,
			remember=body.remember,
		)
		_set_session_cookie(response, signed)
		return {"user": {"username": signed.username}, "csrf": signed.csrf_token}

	@app.post("/v1/user/logout")
	async def logout(
		response: Response,
		ctx: UserContext = SessionDep,
		x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
	) -> dict:
		"""Drop the session and clear the cookie."""
		check_csrf(ctx, x_csrf_token)
		await auth.sign_out(ctx.registry, ctx.session_token)
		response.delete_cookie(SESSION_COOKIE, path="/")
		return {"ok": True}

	async def user_engine(ctx: UserContext = SessionDep) -> AsyncIterator[AsyncEngine]:
		"""Open the session user's own database for one request."""
		user = await registry_ops.get_user_by_id(ctx.registry, ctx.user.user_id)
		if user is None:
			raise ApiError(401, "unauthorized", "the user is gone")
		engine = create_sqlite_engine(user_db_path(data_dir, user.directory))
		try:
			yield engine
		finally:
			await engine.dispose()

	UserEngineDep = Depends(user_engine)

	def _wallet_json(wallet: wallet_ops.WalletInfo) -> dict:
		return {
			"id": wallet.id,
			"family": wallet.family,
			"xpub": wallet.xpub,
			"label": wallet.label,
			"addresses": wallet.addresses,
			"created_at": wallet.created_at.isoformat() if wallet.created_at else None,
		}

	@app.get("/v1/user/wallets")
	async def list_wallets(
		ctx: UserContext = SessionDep, engine: AsyncEngine = UserEngineDep
	) -> dict:
		"""The user's wallets with bound address counts."""
		return {"wallets": [_wallet_json(w) for w in await wallet_ops.list_wallets(engine)]}

	@app.post("/v1/user/wallets")
	async def attach_wallet(
		body: AttachWalletRequest,
		ctx: UserContext = SessionDep,
		engine: AsyncEngine = UserEngineDep,
		x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
	) -> dict:
		"""Attach a watch-only wallet (the recommended path of ADR-0002)."""
		check_csrf(ctx, x_csrf_token)
		wallet = await wallet_ops.attach_wallet(
			engine, family=body.family, xpub=body.xpub, label=body.label
		)
		return {"wallet": _wallet_json(wallet)}

	@app.post("/v1/user/wallets/generate")
	async def generate_wallet(
		body: GenerateWalletRequest,
		ctx: UserContext = SessionDep,
		x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
	) -> dict:
		"""One-time seed generation: the phrase is returned once, stored never."""
		check_csrf(ctx, x_csrf_token)
		material = wallet_ops.generate_material(
			words=body.words, families=body.families, passphrase=body.passphrase
		)
		return {
			"phrase": material.phrase.split(" "),
			"wallets": [{"family": family, "xpub": xpub} for family, xpub in material.xpubs],
		}

	@app.get("/v1/user/me")
	async def me(ctx: UserContext = SessionDep) -> dict:
		"""The signed-in user, their emails and the CSRF token for this session."""
		emails = await registry_ops.list_user_emails(ctx.registry, ctx.user.user_id)
		return {
			"user": {
				"username": ctx.user.username,
				"emails": [
					{
						"address": e.address,
						"primary": e.is_primary,
						"confirmed": e.confirmed_at is not None,
					}
					for e in emails
				],
			},
			"csrf": ctx.user.csrf_token,
		}
