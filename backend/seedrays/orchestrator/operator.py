"""Operator panel operations: sign-in, gateway users, gateway settings.

The operator route group of ADR-0004/0005: manages users and gateway-wide
settings. Operator accounts are created only from the server console
(``seedrays operator-create``) — there is no operator registration.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains
from seedrays.orchestrator.operations import OperationError
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_wallets
from seedrays.storage.engine import create_sqlite_engine, now_utc, user_db_path

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()

_LOGIN_RE = re.compile(r"^\S{3,64}$")
_MIN_PASSWORD_LEN = 8

SESSION_DAYS = 1  # операторская сессия короче пользовательской

USER_STATUS_ACTIVE = "active"
USER_STATUS_BLOCKED = "blocked"

# Настройки, которыми управляет панель. Секретные значения наружу не
# возвращаются никогда — только признак «задано»; пустая строка при
# сохранении означает «не менять».
SETTING_FIELDS = (
	{"key": "provider.trongrid.api_key", "secret": True},
	{"key": "provider.trongrid.rate_per_sec", "secret": False},
	{"key": "watcher.interval_seconds", "secret": False},
	{"key": "watcher.overlap_minutes", "secret": False},
	{"key": "mail.resend.api_key", "secret": True},
	{"key": "mail.from", "secret": False},
	{"key": "gateway.base_url", "secret": False},
	{"key": "mail.dev_autoconfirm", "secret": False},
)
_SECRET_KEYS = {field["key"] for field in SETTING_FIELDS if field["secret"]}
_KNOWN_KEYS = {field["key"] for field in SETTING_FIELDS}


def _sha256(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


async def create_operator(registry: AsyncEngine, *, login: str, password: str) -> int:
	"""Create an operator account (the console bootstrap path).

	Raises:
		OperationError: invalid_username / weak_password / username_taken.
	"""
	if not _LOGIN_RE.fullmatch(login):
		raise OperationError(
			"invalid_username", "operator login must be 3-64 characters without spaces"
		)
	if len(password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	try:
		return await registry_ops.create_operator(registry, login, _hasher.hash(password))
	except ValueError as exc:
		raise OperationError("username_taken", "this operator login is already taken") from exc


@dataclass(frozen=True)
class OperatorSignedIn:
	"""Outcome of a successful operator sign-in."""

	operator_id: int
	login: str
	session_token: str
	csrf_token: str
	expires_at: datetime


@dataclass(frozen=True)
class CurrentOperator:
	"""The session's operator, resolved from the cookie token."""

	operator_id: int
	login: str
	csrf_token: str


async def sign_in(registry: AsyncEngine, *, login: str, password: str) -> OperatorSignedIn:
	"""Operator sign-in; issues a panel session with a CSRF token.

	Raises:
		OperationError: invalid_credentials.
	"""
	operator = await registry_ops.get_operator_by_login(registry, login.strip())
	if operator is None or operator.status != "active":
		raise OperationError("invalid_credentials", "wrong login or password")
	try:
		_hasher.verify(operator.password_hash, password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		raise OperationError("invalid_credentials", "wrong login or password") from exc

	token = secrets.token_urlsafe(32)
	csrf = secrets.token_urlsafe(32)
	expires = now_utc() + timedelta(days=SESSION_DAYS)
	await registry_ops.create_operator_session(
		registry,
		operator_id=operator.id,
		token_hash=_sha256(token),
		csrf_token=csrf,
		expires_at=expires,
	)
	return OperatorSignedIn(
		operator_id=operator.id,
		login=operator.login,
		session_token=token,
		csrf_token=csrf,
		expires_at=expires,
	)


async def resolve_session(
	registry: AsyncEngine, session_token: str
) -> CurrentOperator | None:
	"""The session's operator, or None when the token is unknown or expired."""
	row = await registry_ops.get_operator_session_by_token_hash(
		registry, _sha256(session_token), now=now_utc()
	)
	if row is None:
		return None
	operator = await registry_ops.get_operator_by_id(registry, row.operator_id)
	if operator is None or operator.status != "active":
		return None
	return CurrentOperator(
		operator_id=operator.id, login=operator.login, csrf_token=row.csrf_token
	)


async def sign_out(registry: AsyncEngine, session_token: str) -> None:
	"""Drop the operator session."""
	await registry_ops.delete_operator_session(registry, _sha256(session_token))


async def change_password(
	registry: AsyncEngine,
	*,
	operator_id: int,
	current_password: str,
	new_password: str,
	session_token: str,
) -> None:
	"""Change the operator's password; other operator sessions are dropped.

	Raises:
		OperationError: invalid_credentials / weak_password.
	"""
	if len(new_password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	operator = await registry_ops.get_operator_by_id(registry, operator_id)
	if operator is None:
		raise OperationError("invalid_credentials", "wrong current password")
	try:
		_hasher.verify(operator.password_hash, current_password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		raise OperationError("invalid_credentials", "wrong current password") from exc
	await registry_ops.set_operator_password(
		registry, operator_id, _hasher.hash(new_password)
	)
	await registry_ops.delete_operator_sessions(
		registry, operator_id, keep_token_hash=_sha256(session_token)
	)


@dataclass(frozen=True)
class GatewayUser:
	"""One gateway user of the operator's list."""

	id: int
	username: str
	status: str
	emails: list[dict]
	wallets: int | None  # None — база пользователя не прочиталась
	created_at: str | None


async def list_gateway_users(registry: AsyncEngine, data_dir: Path) -> list[GatewayUser]:
	"""Every gateway user with emails and wallet counts.

	Сломанная база одного пользователя не валит список — у такой строки
	вместо числа кошельков «неизвестно» (None) и запись в журнале.
	"""
	users = await registry_ops.list_users(registry)
	emails = await registry_ops.list_emails_by_users(registry, [u.id for u in users])
	result: list[GatewayUser] = []
	for user in users:
		wallet_count: int | None = None
		db_path = user_db_path(data_dir, user.directory)
		if db_path.exists():
			engine = create_sqlite_engine(db_path)
			try:
				wallet_count = len(await user_wallets.list_wallets(engine))
			except SQLAlchemyError:
				logger.exception("user %s: database unreadable for the operator list", user.login)
			finally:
				await engine.dispose()
		result.append(
			GatewayUser(
				id=user.id,
				username=user.login,
				status=user.status,
				emails=[
					{
						"address": e.address,
						"primary": e.is_primary,
						"confirmed": e.confirmed_at is not None,
					}
					for e in emails.get(user.id, [])
				],
				wallets=wallet_count,
				created_at=user.created_at.isoformat() if user.created_at else None,
			)
		)
	return result


async def set_user_status(registry: AsyncEngine, *, user_id: int, status: str) -> None:
	"""Block or unblock a gateway user; blocking drops the user's sessions.

	Raises:
		OperationError: invalid_status / unknown_user.
	"""
	if status not in (USER_STATUS_ACTIVE, USER_STATUS_BLOCKED):
		raise OperationError("invalid_status", "status must be 'active' or 'blocked'")
	if await registry_ops.get_user_by_id(registry, user_id) is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	await registry_ops.set_user_status(registry, user_id, status)
	if status == USER_STATUS_BLOCKED:
		await registry_ops.delete_user_sessions(registry, user_id)


async def reset_user_password(registry: AsyncEngine, *, user_id: int) -> str:
	"""Set a random password for a user; returned once, all their sessions die.

	Запасной путь восстановления из сценария кабинета: пользователь потерял
	и пароль, и почту — оператор выдаёт временный пароль вне шлюза.

	Raises:
		OperationError: unknown_user.
	"""
	if await registry_ops.get_user_by_id(registry, user_id) is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	password = secrets.token_urlsafe(9)  # 12 знаков — временный, под смену
	await registry_ops.set_user_password(registry, user_id, _hasher.hash(password))
	await registry_ops.delete_user_sessions(registry, user_id)
	return password


async def get_settings(registry: AsyncEngine) -> list[dict]:
	"""The panel's settings: values for plain keys, only a flag for secrets."""
	result = []
	for field in SETTING_FIELDS:
		value = await registry_ops.get_setting(registry, field["key"])
		is_set = bool(value)
		result.append(
			{
				"key": field["key"],
				"secret": field["secret"],
				"set": is_set,
				"value": None if field["secret"] else (value or ""),
			}
		)
	return result


async def update_settings(registry: AsyncEngine, values: dict[str, str]) -> None:
	"""Store the submitted settings.

	Для секретных ключей пустая строка означает «не менять» (форма не
	видит текущее значение); для остальных пустая строка сохраняется как
	есть — это явное «настройка снята».

	Raises:
		OperationError: unknown_setting.
	"""
	for key, value in values.items():
		if key not in _KNOWN_KEYS:
			raise OperationError("unknown_setting", f"unknown setting {key!r}")
		if key in _SECRET_KEYS and value == "":
			continue
		await registry_ops.set_setting(registry, key, value.strip())


async def watcher_status(registry: AsyncEngine) -> list[dict]:
	"""Per-network watcher cursors for the read-only panel block."""
	result = []
	for network in sorted(chains.supported_networks()):
		state = await registry_ops.get_watcher_state(registry, network)
		result.append(
			{
				"network": network,
				"last_block": state.last_block if state else None,
				"last_scan_at": (
					state.last_scan_at.isoformat(sep=" ", timespec="minutes")
					if state and state.last_scan_at
					else None
				),
			}
		)
	return result
