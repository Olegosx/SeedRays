"""Cabinet authentication operations: registration, sign-in, sessions.

Business rules of the sign-in scenario (docs/50-frontend/user-cabinet.md):
username + email + password registration with email confirmation, sign-in
by one identifier field (username or email), revocable registry sessions
with a CSRF token.
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
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.mail.base import MailError, MailSender
from seedrays.orchestrator.operations import OperationError
from seedrays.orchestrator.seclog import ACTOR_USER, OUTCOME_SUCCESS, SecurityLog
from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import now_utc

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()

# Имя пользователя: 3–64 знака, без пробелов и без @ (иначе имя могло бы
# совпасть с чужой почтой в общем поле входа — см. сценарий кабинета).
_USERNAME_RE = re.compile(r"^[^@\s]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LEN = 8

SESSION_DAYS = 7
SESSION_DAYS_REMEMBER = 30
CONFIRM_TOKEN_HOURS = 24
RESET_TOKEN_HOURS = 1


def _sha256(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class RegisteredUser:
	"""Outcome of a registration."""

	user_id: int
	username: str
	email: str
	confirmation_required: bool


@dataclass(frozen=True)
class SignedIn:
	"""Outcome of a successful sign-in."""

	user_id: int
	username: str
	session_token: str
	csrf_token: str
	expires_at: datetime


@dataclass(frozen=True)
class CurrentUser:
	"""The session's user, resolved from the cookie token."""

	user_id: int
	username: str
	csrf_token: str


async def register(
	registry: AsyncEngine,
	data_dir: Path,
	*,
	username: str,
	email: str,
	password: str,
	mailer: MailSender | None,
	confirm_base_url: str,
	dev_autoconfirm: bool = False,
) -> RegisteredUser:
	"""Create an account: user row, own database, primary email.

	With a mail sender configured the primary email gets a confirmation
	token and a message. Without one the outcome is explicit: the
	development mode (``dev_autoconfirm``) confirms the email immediately
	with a warning in the log; otherwise registration is refused — a
	silent fall-back would let anyone claim a stranger's address.

	Raises:
		OperationError: invalid_username / invalid_email / weak_password /
			username_taken / email_taken / mail_not_configured.
	"""
	if not _USERNAME_RE.fullmatch(username):
		raise OperationError(
			"invalid_username",
			"username must be 3-64 characters without spaces or '@'",
		)
	email = email.strip().lower()
	if not _EMAIL_RE.fullmatch(email):
		raise OperationError("invalid_email", "email address looks invalid")
	if len(password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	if await registry_ops.get_email_by_address(registry, email) is not None:
		raise OperationError("email_taken", "this email is already attached to an account")

	password_hash = _hasher.hash(password)
	try:
		user = await registry_ops.create_user(registry, data_dir, username, password_hash)
	except ValueError as exc:
		raise OperationError("username_taken", "this username is already taken") from exc

	try:
		required = await _attach_email(
			registry,
			user_id=user.id,
			address=email,
			is_primary=True,
			mailer=mailer,
			confirm_base_url=confirm_base_url,
			dev_autoconfirm=dev_autoconfirm,
			message_intro="Follow the link to confirm your email and finish the registration:",
		)
	except BaseException:
		# Компенсация: без подтверждаемой почты учётка мертва (вход закрыт),
		# а логин и адрес остались бы занятыми навсегда. Пустой каталог
		# пользователя на диске остаётся сиротой — это безвредно.
		await registry_ops.delete_user_record(registry, user.id)
		raise
	return RegisteredUser(
		user_id=user.id, username=user.login, email=email, confirmation_required=required
	)


async def _attach_email(
	registry: AsyncEngine,
	*,
	user_id: int,
	address: str,
	is_primary: bool,
	mailer: MailSender | None,
	confirm_base_url: str,
	dev_autoconfirm: bool,
	message_intro: str,
) -> bool:
	"""Attach one address: the single confirmation scenario of both flows.

	Без отправителя исход явный: режим разработки авто-подтверждает адрес
	с предупреждением в журнале, иначе операция отклоняется. С отправителем
	пишется отпечаток токена и уходит письмо со ссылкой подтверждения.

	Returns:
		True when confirmation by email is required.

	Raises:
		OperationError: mail_not_configured / mail_failed.
	"""
	if mailer is None:
		if not dev_autoconfirm:
			raise OperationError(
				"mail_not_configured",
				"outgoing mail is not configured; ask the operator to set it up "
				"(or to enable the development auto-confirm mode)",
			)
		# Явный режим разработки: почта авто-подтверждается с предупреждением.
		logger.warning("development mail mode: email %s auto-confirmed", address)
		await registry_ops.add_user_email(
			registry,
			user_id=user_id,
			address=address,
			is_primary=is_primary,
			confirm_token_hash=None,
			confirm_expires_at=None,
			confirmed_at=now_utc(),
		)
		return False

	token = secrets.token_urlsafe(32)
	await registry_ops.add_user_email(
		registry,
		user_id=user_id,
		address=address,
		is_primary=is_primary,
		confirm_token_hash=_sha256(token),
		confirm_expires_at=now_utc() + timedelta(hours=CONFIRM_TOKEN_HOURS),
	)
	link = f"{confirm_base_url.rstrip('/')}/v1/user/confirm-email?token={token}"
	try:
		await mailer.send(
			address,
			"SeedRays: confirm your email",
			f"{message_intro}\n{link}\n\nThe link is valid for {CONFIRM_TOKEN_HOURS} hours.",
		)
	except MailError as exc:
		logger.error("confirmation email to %s failed: %s", address, exc)
		raise OperationError(
			"mail_failed", "could not send the confirmation email; try again later"
		) from exc
	return True


async def confirm_email(registry: AsyncEngine, token: str) -> bool:
	"""Confirm an email by its token; False when the token is unknown/expired."""
	record = await registry_ops.confirm_email_by_token_hash(
		registry, _sha256(token), now=now_utc()
	)
	return record is not None


async def sign_in(
	registry: AsyncEngine,
	*,
	identifier: str,
	password: str,
	remember: bool,
	client: str | None = None,
	seclog: SecurityLog | None = None,
) -> SignedIn:
	"""Sign in by username or email; issues a session with a CSRF token.

	Точная причина отказа («нет пользователя», «заблокирован», «не тот
	пароль») уходит в журнал безопасности здесь — наружу все три отвечают
	одинаковым invalid_credentials (ADR-0023).

	Raises:
		OperationError: invalid_credentials / email_not_confirmed.
	"""
	await registry_ops.delete_expired_sessions(registry, now=now_utc())

	identifier = identifier.strip()

	async def _journal(outcome: str, user_id: int | None = None) -> None:
		if seclog is not None:
			await seclog.event(
				registry,
				"login",
				actor=ACTOR_USER,
				outcome=outcome,
				identifier=identifier,
				user_id=user_id,
				client=client,
			)

	user = await registry_ops.get_user_by_login(registry, identifier)
	if user is None:
		email = await registry_ops.get_email_by_address(registry, identifier.lower())
		if email is not None:
			user = await registry_ops.get_user_by_id(registry, email.user_id)
	if user is None:
		await _journal("unknown_identifier")
		# Одинаковый ответ для «нет пользователя» и «не тот пароль».
		raise OperationError("invalid_credentials", "wrong username/email or password")
	if user.status != "active":
		await _journal("user_blocked", user.id)
		raise OperationError("invalid_credentials", "wrong username/email or password")
	try:
		_hasher.verify(user.password_hash, password)
	except InvalidHashError as exc:
		# Сохранённый хеш не разбирается — введённый пароль тут ни при чём.
		# Под исходом «неверный пароль» это выглядело бы как забывчивость
		# пользователя, и настоящая причина не была бы видна нигде
		# (ADR-0023: причина записывается там, где она ещё известна).
		logger.error("user %d: the stored password hash is unusable", user.id)
		await _journal("broken_password_hash", user.id)
		raise OperationError(
			"invalid_credentials", "wrong username/email or password"
		) from exc
	except VerifyMismatchError as exc:
		await _journal("wrong_password", user.id)
		raise OperationError(
			"invalid_credentials", "wrong username/email or password"
		) from exc

	emails = await registry_ops.list_user_emails(registry, user.id)
	primary = next((e for e in emails if e.is_primary), None)
	if primary is not None and primary.confirmed_at is None:
		await _journal("email_not_confirmed", user.id)
		raise OperationError(
			"email_not_confirmed", "confirm your email first (check your inbox)"
		)
	await _journal(OUTCOME_SUCCESS, user.id)

	token = secrets.token_urlsafe(32)
	csrf = secrets.token_urlsafe(32)
	days = SESSION_DAYS_REMEMBER if remember else SESSION_DAYS
	expires = now_utc() + timedelta(days=days)
	await registry_ops.create_session(
		registry,
		user_id=user.id,
		token_hash=_sha256(token),
		csrf_token=csrf,
		expires_at=expires,
	)
	return SignedIn(
		user_id=user.id,
		username=user.login,
		session_token=token,
		csrf_token=csrf,
		expires_at=expires,
	)


async def resolve_session(registry: AsyncEngine, session_token: str) -> CurrentUser | None:
	"""The session's user, or None when the token is unknown or expired."""
	session = await registry_ops.get_session_by_token_hash(
		registry, _sha256(session_token), now=now_utc()
	)
	if session is None:
		return None
	user = await registry_ops.get_user_by_id(registry, session.user_id)
	if user is None or user.status != "active":
		return None
	return CurrentUser(user_id=user.id, username=user.login, csrf_token=session.csrf_token)


async def sign_out(registry: AsyncEngine, session_token: str) -> None:
	"""Drop the session."""
	await registry_ops.delete_session(registry, _sha256(session_token))


async def add_email(
	registry: AsyncEngine,
	*,
	user_id: int,
	address: str,
	mailer: MailSender | None,
	confirm_base_url: str,
	dev_autoconfirm: bool = False,
) -> bool:
	"""Attach a secondary email; returns True when confirmation is required.

	Without a mail sender the outcome mirrors registration: the explicit
	development mode auto-confirms with a warning, otherwise the
	operation is refused.

	Raises:
		OperationError: invalid_email / email_taken / mail_failed /
			mail_not_configured.
	"""
	address = address.strip().lower()
	if not _EMAIL_RE.fullmatch(address):
		raise OperationError("invalid_email", "email address looks invalid")
	if await registry_ops.get_email_by_address(registry, address) is not None:
		raise OperationError("email_taken", "this email is already attached to an account")

	return await _attach_email(
		registry,
		user_id=user_id,
		address=address,
		is_primary=False,
		mailer=mailer,
		confirm_base_url=confirm_base_url,
		dev_autoconfirm=dev_autoconfirm,
		message_intro="Follow the link to confirm this email address:",
	)


async def remove_email(registry: AsyncEngine, *, user_id: int, email_id: int) -> None:
	"""Detach a secondary email.

	Raises:
		OperationError: unknown_email / cannot_remove_primary.
	"""
	record = await registry_ops.get_email_by_id(registry, email_id)
	if record is None or record.user_id != user_id:
		raise OperationError("unknown_email", "no such email on this account")
	if record.is_primary:
		raise OperationError("cannot_remove_primary", "the primary email cannot be removed")
	await registry_ops.delete_user_email(registry, email_id)


async def request_password_reset(
	registry: AsyncEngine,
	*,
	email: str,
	mailer: MailSender | None,
	reset_base_url: str,
	client: str | None = None,
	seclog: SecurityLog | None = None,
) -> None:
	"""Send a password-reset link to a registered, confirmed email.

	Ответ наружу всегда одинаковый (маршрут не раскрывает, существует ли
	адрес), поэтому «адрес неизвестен» и «адрес не подтверждён» завершаются
	молча — с точной отметкой в журнале безопасности (ADR-0023). Токен
	одноразовый, живёт :data:`RESET_TOKEN_HOURS` часов; новый запрос
	вытесняет прежний токен.

	Raises:
		OperationError: mail_not_configured / mail_failed — сбои шлюза,
			не раскрывающие ничего об адресе.
	"""
	address = email.strip().lower()

	async def _journal(outcome: str, user_id: int | None = None) -> None:
		if seclog is not None:
			await seclog.event(
				registry,
				"password_reset_request",
				actor=ACTOR_USER,
				outcome=outcome,
				identifier=address,
				user_id=user_id,
				client=client,
			)

	if mailer is None:
		# Сброс без письма невозможен по сути: подтверждать личность нечем.
		# Явный отказ и в режиме разработки — «сбросить кому угодно» дырой
		# быть не должно.
		raise OperationError(
			"mail_not_configured",
			"outgoing mail is not configured; ask the operator to set it up",
		)
	record = await registry_ops.get_email_by_address(registry, address)
	if record is None:
		logger.info("password reset requested for an unknown email")
		await _journal("unknown_email")
		return
	if record.confirmed_at is None:
		# Неподтверждённый адрес мог вписать кто угодно — писать на него нельзя.
		logger.info("password reset requested for an unconfirmed email, ignored")
		await _journal("unconfirmed_email", record.user_id)
		return

	token = secrets.token_urlsafe(32)
	await registry_ops.set_password_reset(
		registry,
		user_id=record.user_id,
		token_hash=_sha256(token),
		expires_at=now_utc() + timedelta(hours=RESET_TOKEN_HOURS),
	)
	link = f"{reset_base_url.rstrip('/')}/password-new.html?token={token}"
	try:
		await mailer.send(
			address,
			"SeedRays: password reset",
			"Follow the link to set a new password:\n"
			f"{link}\n\nThe link is valid for {RESET_TOKEN_HOURS} hour(s). "
			"If you did not request a reset, ignore this message.",
		)
	except MailError as exc:
		logger.error("password reset email to %s failed: %s", address, exc)
		raise OperationError(
			"mail_failed", "could not send the reset email; try again later"
		) from exc
	await _journal(OUTCOME_SUCCESS, record.user_id)


async def reset_password(registry: AsyncEngine, *, token: str, new_password: str) -> int:
	"""Set a new password by a one-time reset token; drops every session.

	Returns:
		The id of the user whose password was reset (for the journal).

	Raises:
		OperationError: weak_password / invalid_token.
	"""
	if len(new_password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	user_id = await registry_ops.consume_password_reset(
		registry, _sha256(token), now=now_utc()
	)
	if user_id is None:
		raise OperationError("invalid_token", "the reset link is invalid or expired")
	await registry_ops.set_user_password(registry, user_id, _hasher.hash(new_password))
	# Пароль сброшен из-за потери доступа — все прежние сессии гасятся.
	await registry_ops.delete_user_sessions(registry, user_id)
	return user_id


async def change_password(
	registry: AsyncEngine,
	*,
	user_id: int,
	current_password: str,
	new_password: str,
	session_token: str,
) -> None:
	"""Change the password; every other session of the user is dropped.

	Raises:
		OperationError: invalid_credentials / weak_password.
	"""
	if len(new_password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	user = await registry_ops.get_user_by_id(registry, user_id)
	if user is None:
		raise OperationError("invalid_credentials", "wrong current password")
	try:
		_hasher.verify(user.password_hash, current_password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		raise OperationError("invalid_credentials", "wrong current password") from exc
	await registry_ops.set_user_password(registry, user_id, _hasher.hash(new_password))
	await registry_ops.delete_user_sessions(
		registry, user_id, keep_token_hash=_sha256(session_token)
	)
