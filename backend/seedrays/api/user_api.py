"""The User API route group: cabinet authentication (ADR-0004, ADR-0005).

A thin adapter over the orchestrator auth operations. The session rides
in an HttpOnly cookie; mutating requests must carry the X-CSRF-Token
header matching the session's CSRF token.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.api.errors import ApiError
from seedrays.orchestrator.operations import OperationError
from seedrays.chains import explorer_tx_url, supported_networks
from seedrays.families import Family
from seedrays.mail.base import MailSender
from seedrays.mail.resend import ResendSender
from seedrays.orchestrator import apps as app_ops
from seedrays.orchestrator import auth
from seedrays.orchestrator import overview as overview_ops
from seedrays.orchestrator import wallets as wallet_ops
from seedrays.orchestrator.captcha import CaptchaGuard
from seedrays.orchestrator.ratelimit import RateLimiter
from seedrays.orchestrator.seclog import ACTOR_USER, OUTCOME_SUCCESS, SecurityLog
from seedrays.storage import billing as billing_store
from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import (
	billing_db_path,
	create_sqlite_engine,
	now_utc,
	registry_db_path,
	user_db_path,
)

logger = logging.getLogger(__name__)

SESSION_COOKIE = "seedrays_session"

# Ключи настроек почты (реестр, ADR-0016).
SETTING_MAIL_API_KEY = "mail.resend.api_key"
SETTING_MAIL_FROM = "mail.from"
SETTING_BASE_URL = "gateway.base_url"
# Явный режим разработки: без отправителя почты адреса авто-подтверждаются
# только при включённом флаге — молчаливый «fail-open» недопустим.
SETTING_MAIL_DEV = "mail.dev_autoconfirm"

# Тормоз перебора (скользящие окна в памяти процесса, ADR-0003).
LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60
REGISTER_LIMIT = 10
REGISTER_WINDOW_SECONDS = 60 * 60
RESET_LIMIT = 5
RESET_WINDOW_SECONDS = 60 * 60


class RegisterRequest(BaseModel):
	"""Body of the registration operation."""

	username: str = Field(min_length=1, max_length=128)
	email: str = Field(min_length=3, max_length=255)
	password: str = Field(min_length=1, max_length=1024)
	captcha: str = Field(min_length=1, max_length=4096)


class LoginRequest(BaseModel):
	"""Body of the sign-in operation."""

	identifier: str = Field(min_length=1, max_length=255)
	password: str = Field(min_length=1, max_length=1024)
	remember: bool = False
	captcha: str = Field(min_length=1, max_length=4096)


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


class ResetRequest(BaseModel):
	"""Body of the password-reset request."""

	email: str = Field(min_length=3, max_length=255)
	captcha: str = Field(min_length=1, max_length=4096)


class ResetConfirmRequest(BaseModel):
	"""Body of the password-reset confirmation."""

	token: str = Field(min_length=1, max_length=128)
	new_password: str = Field(min_length=1, max_length=1024)


class AddEmailRequest(BaseModel):
	"""Body of the add-email operation."""

	address: str = Field(min_length=3, max_length=255)


class ChangePasswordRequest(BaseModel):
	"""Body of the change-password operation."""

	current_password: str = Field(min_length=1, max_length=1024)
	new_password: str = Field(min_length=1, max_length=1024)


class CreateApplicationRequest(BaseModel):
	"""Body of the create-application operation."""

	name: str = Field(min_length=1, max_length=64)


class IssueAddressesRequest(BaseModel):
	"""Body of the cabinet issue-addresses operation."""

	networks: list[str] | Literal["all"] = Field(
		description="Network codes, or 'all' for every configured network"
	)


class NetworkMappingRequest(BaseModel):
	"""Body of the network→wallet mapping operation."""

	network: str = Field(min_length=1, max_length=32)
	wallet_id: int


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
	app: FastAPI,
	data_dir: Path,
	mailer: MailSender | None = None,
	captcha_cost: int | None = None,
	seclog: SecurityLog | None = None,
) -> None:
	"""Attach the /v1/user route group.

	Args:
		app: The FastAPI application.
		data_dir: The gateway data directory.
		mailer: Mail sender override (tests); by default the sender is
			built from the registry settings on every mail-sending
			operation (registration, adding an email).
		captcha_cost: Proof-of-work cost override (tests); by default
			the production cost of the captcha module.
		seclog: The gateway's security journal; created here when the
			caller did not pass the shared instance.
	"""
	login_limiter = RateLimiter(LOGIN_LIMIT, LOGIN_WINDOW_SECONDS)
	register_limiter = RateLimiter(REGISTER_LIMIT, REGISTER_WINDOW_SECONDS)
	reset_limiter = RateLimiter(RESET_LIMIT, RESET_WINDOW_SECONDS)
	captcha_guard = CaptchaGuard(cost=captcha_cost) if captcha_cost else CaptchaGuard()
	journal = seclog if seclog is not None else SecurityLog(data_dir)

	async def _check_captcha(
		payload: str,
		*,
		registry: AsyncEngine,
		event: str,
		identifier: str | None,
		client: str,
	) -> None:
		"""Reject the request when its proof-of-work solution is not genuine.

		Отказ капчи — событие журнала безопасности (ADR-0023).
		"""
		if not captcha_guard.verify(payload):
			await journal.event(
				registry, event, actor=ACTOR_USER, outcome="captcha_failed",
				identifier=identifier, client=client,
			)
			raise ApiError(
				400, "captcha_failed", "the proof-of-work check failed; please try again"
			)

	def _client_host(request: Request) -> str:
		"""The caller's address for rate-limit keys."""
		return request.client.host if request.client else "unknown"

	async def _mail_context(registry: AsyncEngine) -> tuple[MailSender | None, str, bool]:
		"""Resolve the mail sender, the confirmation base URL and the dev mode.

		Ссылка подтверждения строится только из настройки
		``gateway.base_url`` — заголовку Host запроса доверять нельзя
		(подменённый Host увёл бы токен подтверждения на чужой домен).
		Отправитель из настроек без базового адреса не используется.
		"""
		base_url = await registry_ops.get_setting(registry, SETTING_BASE_URL) or ""
		if mailer is not None:
			active: MailSender | None = mailer
		else:
			active = await _resolve_mailer(registry)
			if active is not None and not base_url:
				logger.error(
					"mail sender is configured but %s is not set; mail disabled",
					SETTING_BASE_URL,
				)
				active = None
		dev_raw = await registry_ops.get_setting(registry, SETTING_MAIL_DEV)
		dev = (dev_raw or "").strip().lower() in ("1", "true", "yes")
		return active, base_url, dev

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

	async def payment_session(
		request: Request, registry: AsyncEngine = RegistryDep
	) -> UserContext:
		"""Authenticate the session cookie, without the billing access check.

		Маршруты, которые обязаны работать и у приостановленного за неуплату
		пользователя: сам счёт, чтение учётной записи и выход. Иначе платить
		было бы нечем и не за что (ADR-0027).
		"""
		token = request.cookies.get(SESSION_COOKIE)
		if not token:
			raise ApiError(401, "unauthorized", "sign in first")
		user = await auth.resolve_session(registry, token)
		if user is None:
			raise ApiError(401, "unauthorized", "the session is expired or unknown")
		return UserContext(registry=registry, user=user, session_token=token)

	PaymentSessionDep = Depends(payment_session)

	async def session_user(
		ctx: UserContext = PaymentSessionDep, billing: AsyncEngine = BillingDep
	) -> UserContext:
		"""Authenticate the session and refuse a user suspended for non-payment.

		Проверка живёт в базовой зависимости сознательно: новый маршрут
		кабинета получает её по умолчанию, а чтобы её обойти, нужно осознанно
		взять зависимость оплаты. Забыть — нельзя, можно только отказаться.
		"""
		if await billing_store.access_state(billing, ctx.user.user_id) == (
			billing_store.ACCESS_SUSPENDED
		):
			raise ApiError(
				403,
				"billing_suspended",
				"the gateway fee invoice is overdue; settle it to restore access",
			)
		return ctx

	SessionDep = Depends(session_user)

	def _require_csrf(request: Request, ctx: UserContext) -> UserContext:
		"""Сравнение токена — за постоянное время, чтобы не давать оракула."""
		header_token = request.headers.get("X-CSRF-Token")
		if not header_token or not hmac.compare_digest(header_token, ctx.user.csrf_token):
			raise ApiError(403, "csrf", "the X-CSRF-Token header is missing or wrong")
		return ctx

	async def mutating_session(
		request: Request, ctx: UserContext = SessionDep
	) -> UserContext:
		"""Session + CSRF check in one dependency.

		Каждый изменяющий маршрут объявляет эту зависимость и тем самым
		защищён по построению (структурная граница ADR-0004): забыть
		проверку, добавив маршрут, невозможно.
		"""
		return _require_csrf(request, ctx)

	MutatingSessionDep = Depends(mutating_session)

	async def payment_mutating(
		request: Request, ctx: UserContext = PaymentSessionDep
	) -> UserContext:
		"""CSRF check for the routes a suspended user must still be able to call."""
		return _require_csrf(request, ctx)

	PaymentMutatingDep = Depends(payment_mutating)

	def _set_session_cookie(response: Response, signed: auth.SignedIn) -> None:
		max_age = int((signed.expires_at - now_utc()).total_seconds())
		# Secure: токен сессии не должен уходить по нешифрованному каналу
		# (threat-model требует «HTTPS only»); локальная разработка не
		# страдает — localhost браузеры считают доверенным источником.
		response.set_cookie(
			SESSION_COOKIE,
			signed.session_token,
			max_age=max_age,
			httponly=True,
			secure=True,
			samesite="lax",
			path="/",
		)

	@app.get("/v1/user/captcha")
	async def issue_captcha() -> dict:
		"""One signed proof-of-work challenge for the ALTCHA widget."""
		return captcha_guard.issue()

	@app.post("/v1/user/register")
	async def register(
		body: RegisterRequest, request: Request, registry: AsyncEngine = RegistryDep
	) -> dict:
		"""Create an account; sends the confirmation email when mail is set up."""
		client = _client_host(request)
		if not register_limiter.allow(client):
			await journal.event(
				registry, "register", actor=ACTOR_USER, outcome="rate_limited",
				identifier=body.username, client=client,
			)
			raise ApiError(429, "rate_limited", "too many registrations; try again later")
		await _check_captcha(
			body.captcha, registry=registry, event="register",
			identifier=body.username, client=client,
		)
		active_mailer, base_url, dev = await _mail_context(registry)
		try:
			registered = await auth.register(
				registry,
				data_dir,
				username=body.username,
				email=body.email,
				password=body.password,
				mailer=active_mailer,
				confirm_base_url=base_url,
				dev_autoconfirm=dev,
			)
		except OperationError as exc:
			# Точная причина отказа — в журнал; ответ наружу не меняется.
			await journal.event(
				registry, "register", actor=ACTOR_USER, outcome=exc.code,
				identifier=body.username, client=client,
			)
			raise
		await journal.event(
			registry, "register", actor=ACTOR_USER, outcome=OUTCOME_SUCCESS,
			identifier=registered.username, user_id=registered.user_id, client=client,
		)
		return {
			"user": {"username": registered.username},
			"confirmation_required": registered.confirmation_required,
		}

	@app.get("/v1/user/confirm-email")
	async def confirm_email(
		token: str, registry: AsyncEngine = RegistryDep
	) -> RedirectResponse:
		"""Landing point of the link from the confirmation email."""
		confirmed = await auth.confirm_email(registry, token)
		flag = "1" if confirmed else "0"
		return RedirectResponse(f"/login.html?confirmed={flag}", status_code=303)

	@app.post("/v1/user/login")
	async def login(
		body: LoginRequest,
		request: Request,
		response: Response,
		registry: AsyncEngine = RegistryDep,
	) -> dict:
		"""Sign in by username or email; sets the session cookie."""
		client = _client_host(request)
		key = f"{client}|{body.identifier.strip().lower()}"
		if not login_limiter.allow(key):
			await journal.event(
				registry, "login", actor=ACTOR_USER, outcome="rate_limited",
				identifier=body.identifier.strip(), client=client,
			)
			raise ApiError(429, "rate_limited", "too many sign-in attempts; try again later")
		await _check_captcha(
			body.captcha, registry=registry, event="login",
			identifier=body.identifier.strip(), client=client,
		)
		signed = await auth.sign_in(
			registry,
			identifier=body.identifier,
			password=body.password,
			remember=body.remember,
			client=client,
			seclog=journal,
		)
		_set_session_cookie(response, signed)
		return {"user": {"username": signed.username}, "csrf": signed.csrf_token}

	@app.post("/v1/user/password-reset")
	async def request_password_reset(
		body: ResetRequest, request: Request, registry: AsyncEngine = RegistryDep
	) -> dict:
		"""Send the reset link; the answer never reveals whether the email exists."""
		client = _client_host(request)
		if not reset_limiter.allow(client):
			await journal.event(
				registry, "password_reset_request", actor=ACTOR_USER,
				outcome="rate_limited", identifier=body.email, client=client,
			)
			raise ApiError(429, "rate_limited", "too many reset requests; try again later")
		await _check_captcha(
			body.captcha, registry=registry, event="password_reset_request",
			identifier=body.email, client=client,
		)
		active_mailer, base_url, _dev = await _mail_context(registry)
		try:
			await auth.request_password_reset(
				registry,
				email=body.email,
				mailer=active_mailer,
				reset_base_url=base_url,
				client=client,
				seclog=journal,
			)
		except OperationError as exc:
			# Сбои почты (mail_not_configured / mail_failed) — тоже события.
			await journal.event(
				registry, "password_reset_request", actor=ACTOR_USER,
				outcome=exc.code, identifier=body.email, client=client,
			)
			raise
		return {"ok": True}

	@app.post("/v1/user/password-reset/confirm")
	async def confirm_password_reset(
		body: ResetConfirmRequest, request: Request, registry: AsyncEngine = RegistryDep
	) -> dict:
		"""Set a new password by the one-time token from the reset email."""
		client = _client_host(request)
		if not reset_limiter.allow(client):
			await journal.event(
				registry, "password_reset_confirm", actor=ACTOR_USER,
				outcome="rate_limited", client=client,
			)
			raise ApiError(429, "rate_limited", "too many reset requests; try again later")
		try:
			user_id = await auth.reset_password(
				registry, token=body.token, new_password=body.new_password
			)
		except OperationError as exc:
			# Сам токен в журнал не пишется — это секрет из письма.
			await journal.event(
				registry, "password_reset_confirm", actor=ACTOR_USER,
				outcome=exc.code, client=client,
			)
			raise
		await journal.event(
			registry, "password_reset_confirm", actor=ACTOR_USER,
			outcome=OUTCOME_SUCCESS, user_id=user_id, client=client,
		)
		return {"ok": True}

	@app.post("/v1/user/logout")
	async def logout(
		response: Response, ctx: UserContext = PaymentMutatingDep
	) -> dict:
		"""Drop the session and clear the cookie."""
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

	@app.get("/v1/user/networks")
	async def list_networks(ctx: UserContext = SessionDep) -> dict:
		"""Supported networks and wallet families for the cabinet's pickers.

		Единая точка правды — бэкенд: фронтенд не держит собственных списков
		сетей и семейств, чтобы новая цепочка не требовала правок разметки.
		"""
		return {
			"networks": [
				{
					"network": network,
					"family": family.value,
					"explorer_tx": explorer_tx_url(network),
				}
				for network, family in sorted(supported_networks().items())
			],
			"families": [family.value for family in Family],
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
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
		billing: AsyncEngine = BillingDep,
	) -> dict:
		"""Attach a watch-only wallet (the recommended path of ADR-0002)."""
		wallet = await wallet_ops.attach_wallet(
			engine,
			ctx.registry,
			billing,
			user_id=ctx.user.user_id,
			family=body.family,
			xpub=body.xpub,
			label=body.label,
		)
		return {"wallet": _wallet_json(wallet)}

	@app.post("/v1/user/wallets/generate")
	async def generate_wallet(
		body: GenerateWalletRequest, ctx: UserContext = MutatingSessionDep
	) -> dict:
		"""One-time seed generation: the phrase is returned once, stored never."""
		material = wallet_ops.generate_material(
			words=body.words, families=body.families, passphrase=body.passphrase
		)
		return {
			"phrase": material.phrase.split(" "),
			"wallets": [{"family": family, "xpub": xpub} for family, xpub in material.xpubs],
		}

	def _app_json(summary: app_ops.AppSummary) -> dict:
		return {
			"id": summary.id,
			"name": summary.name,
			"networks": summary.networks,
			"users": summary.users,
			"key": {
				"prefix": summary.key_prefix,
				"issued_at": summary.key_issued_at.isoformat()
				if summary.key_issued_at
				else None,
				"revoked": summary.key_revoked,
			},
			"created_at": summary.created_at.isoformat() if summary.created_at else None,
		}

	@app.get("/v1/user/applications")
	async def list_applications(
		ctx: UserContext = SessionDep, engine: AsyncEngine = UserEngineDep
	) -> dict:
		"""The user's applications."""
		return {
			"applications": [_app_json(a) for a in await app_ops.list_applications(engine)]
		}

	@app.post("/v1/user/applications")
	async def create_application(
		body: CreateApplicationRequest,
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Create an application; the raw key is returned exactly once."""
		summary, key = await app_ops.create_application(
			ctx.registry, engine, user_id=ctx.user.user_id, name=body.name
		)
		return {"application": _app_json(summary), "key": key}

	@app.get("/v1/user/applications/{app_id}")
	async def get_application(
		app_id: int, ctx: UserContext = SessionDep, engine: AsyncEngine = UserEngineDep
	) -> dict:
		"""The application with its network mappings and users."""
		detail = await app_ops.get_application(engine, app_id)
		return {
			"application": _app_json(detail.summary),
			"networks": detail.mappings,
			"app_users": detail.users,
		}

	@app.get("/v1/user/applications/{app_id}/users/{external_id}/addresses")
	async def app_user_addresses(
		app_id: int,
		external_id: str,
		instance: Annotated[str, Query(max_length=64)] = "",
		ctx: UserContext = SessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""The bound addresses of one application user.

		Экземпляр приложения (ADR-0025) — тот же параметр с тем же именем и
		тем же значением по умолчанию, что и в группе приложений.
		"""
		return {
			"addresses": await app_ops.app_user_addresses(
				engine, app_id=app_id, instance=instance, external_id=external_id
			)
		}

	@app.post("/v1/user/applications/{app_id}/users/{external_id}/addresses")
	async def issue_app_user_addresses(
		app_id: int,
		external_id: str,
		body: IssueAddressesRequest,
		instance: Annotated[str, Query(max_length=64)] = "",
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Issue payment addresses for an application user from the cabinet.

		The same idempotent operation the Application API runs; an unseen
		external_id registers the application user implicitly — inside the
		given instance (ADR-0025).
		"""
		return {
			"addresses": await app_ops.issue_addresses(
				engine,
				user_id=ctx.user.user_id,
				app_id=app_id,
				instance=instance,
				external_id=external_id,
				networks=body.networks,
			)
		}

	@app.post("/v1/user/applications/{app_id}/key")
	async def reissue_key(
		app_id: int,
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Reissue the application key; the new raw key is returned exactly once."""
		summary, key = await app_ops.reissue_key(
			ctx.registry, engine, user_id=ctx.user.user_id, app_id=app_id
		)
		return {"application": _app_json(summary), "key": key}

	@app.delete("/v1/user/applications/{app_id}/key")
	async def revoke_key(
		app_id: int,
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Revoke the application key: the application loses API access."""
		summary = await app_ops.revoke_key(ctx.registry, engine, app_id=app_id)
		return {"application": _app_json(summary)}

	@app.put("/v1/user/applications/{app_id}/networks")
	async def set_network_mapping(
		app_id: int,
		body: NetworkMappingRequest,
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Create or replace one "network → wallet" mapping entry."""
		await app_ops.set_network_mapping(
			engine, app_id=app_id, network=body.network, wallet_id=body.wallet_id
		)
		return {"ok": True}

	@app.delete("/v1/user/applications/{app_id}/networks/{network}")
	async def remove_network_mapping(
		app_id: int,
		network: str,
		ctx: UserContext = MutatingSessionDep,
		engine: AsyncEngine = UserEngineDep,
	) -> dict:
		"""Drop one mapping entry."""
		await app_ops.remove_network_mapping(engine, app_id=app_id, network=network)
		return {"ok": True}

	def _history_json(row: overview_ops.HistoryRow) -> dict:
		return {
			"time": row.time,
			"wallet_id": row.wallet_id,
			"wallet": row.wallet,
			"network": row.network,
			"asset": row.asset,
			"amount": row.amount,
			"txid": row.txid,
			"status": row.status,
		}

	@app.get("/v1/user/history")
	async def history(
		ctx: UserContext = SessionDep,
		engine: AsyncEngine = UserEngineDep,
		wallet_id: int | None = None,
		network: str | None = None,
		asset: str | None = None,
		status: str = "all",
		limit: Annotated[int, Query(ge=0)] = overview_ops.HISTORY_LIMIT_DEFAULT,
		cursor: str | None = None,
	) -> dict:
		"""Incoming operations, filterable; ``cursor`` continues the previous page."""
		page = await overview_ops.history(
			engine,
			ctx.registry,
			wallet_id=wallet_id,
			network=network,
			asset=asset,
			status=status,
			limit=limit,
			cursor=overview_ops.parse_history_cursor(cursor) if cursor else None,
		)
		return {
			"history": [_history_json(row) for row in page.rows],
			"next_cursor": (
				f"{page.next_cursor[0]}:{page.next_cursor[1]}" if page.next_cursor else None
			),
		}

	@app.get("/v1/user/overview")
	async def get_overview(
		ctx: UserContext = SessionDep, engine: AsyncEngine = UserEngineDep
	) -> dict:
		"""The dashboard summary: counters, receipts, recent operations."""
		data = await overview_ops.overview(engine, ctx.registry)
		return {
			"counters": {
				"wallets": data.wallets,
				"applications": data.applications,
				"addresses": data.addresses,
			},
			"receipts": data.receipts,
			"recent": [_history_json(row) for row in data.recent],
		}

	@app.get("/v1/user/me")
	async def me(ctx: UserContext = PaymentSessionDep) -> dict:
		"""The signed-in user, their emails and the CSRF token for this session."""
		emails = await registry_ops.list_user_emails(ctx.registry, ctx.user.user_id)
		return {
			"user": {
				"username": ctx.user.username,
				"emails": [
					{
						"id": e.id,
						"address": e.address,
						"primary": e.is_primary,
						"confirmed": e.confirmed_at is not None,
					}
					for e in emails
				],
			},
			"csrf": ctx.user.csrf_token,
		}

	@app.post("/v1/user/emails")
	async def add_email(
		body: AddEmailRequest, ctx: UserContext = MutatingSessionDep
	) -> dict:
		"""Attach a secondary email; it is confirmed by a message."""
		active_mailer, base_url, dev = await _mail_context(ctx.registry)
		required = await auth.add_email(
			ctx.registry,
			user_id=ctx.user.user_id,
			address=body.address,
			mailer=active_mailer,
			confirm_base_url=base_url,
			dev_autoconfirm=dev,
		)
		return {"confirmation_required": required}

	@app.delete("/v1/user/emails/{email_id}")
	async def remove_email(
		email_id: int, ctx: UserContext = MutatingSessionDep
	) -> dict:
		"""Detach a secondary email; the primary one cannot be removed."""
		await auth.remove_email(ctx.registry, user_id=ctx.user.user_id, email_id=email_id)
		return {"ok": True}

	@app.post("/v1/user/password")
	async def change_password(
		body: ChangePasswordRequest,
		request: Request,
		ctx: UserContext = MutatingSessionDep,
	) -> dict:
		"""Change the password; other sessions of the user are dropped."""
		client = _client_host(request)
		try:
			await auth.change_password(
				ctx.registry,
				user_id=ctx.user.user_id,
				current_password=body.current_password,
				new_password=body.new_password,
				session_token=ctx.session_token,
			)
		except OperationError as exc:
			await journal.event(
				ctx.registry, "password_change", actor=ACTOR_USER,
				outcome=exc.code, user_id=ctx.user.user_id, client=client,
			)
			raise
		await journal.event(
			ctx.registry, "password_change", actor=ACTOR_USER,
			outcome=OUTCOME_SUCCESS, user_id=ctx.user.user_id, client=client,
		)
		return {"ok": True}
