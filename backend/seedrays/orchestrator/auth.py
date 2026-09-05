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
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.mail.base import MailError, MailSender
from seedrays.orchestrator.operations import OperationError
from seedrays.storage import registry as registry_ops

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


def _now() -> datetime:
	"""Naive UTC, как во всём слое хранения."""
	return datetime.now(timezone.utc).replace(tzinfo=None)


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
	data_dir,
	*,
	username: str,
	email: str,
	password: str,
	mailer: MailSender | None,
	confirm_base_url: str,
) -> RegisteredUser:
	"""Create an account: user row, own database, primary email.

	With a mail sender configured the primary email gets a confirmation
	token and a message; without one (development mode) the email is
	confirmed immediately and a warning is logged.

	Raises:
		OperationError: invalid_username / invalid_email / weak_password /
			username_taken / email_taken.
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

	token: str | None = None
	if mailer is None:
		# Режим разработки: отправитель почты не настроен — подтверждаем сразу.
		logger.warning("mail sender is not configured; email %s auto-confirmed", email)
		await registry_ops.add_user_email(
			registry,
			user_id=user.id,
			address=email,
			is_primary=True,
			confirm_token_hash=None,
			confirm_expires_at=None,
			confirmed_at=_now(),
		)
		return RegisteredUser(
			user_id=user.id, username=user.login, email=email, confirmation_required=False
		)

	token = secrets.token_urlsafe(32)
	await registry_ops.add_user_email(
		registry,
		user_id=user.id,
		address=email,
		is_primary=True,
		confirm_token_hash=_sha256(token),
		confirm_expires_at=_now() + timedelta(hours=CONFIRM_TOKEN_HOURS),
	)
	link = f"{confirm_base_url.rstrip('/')}/v1/user/confirm-email?token={token}"
	try:
		await mailer.send(
			email,
			"SeedRays: confirm your email",
			"Follow the link to confirm your email and finish the registration:\n"
			f"{link}\n\nThe link is valid for {CONFIRM_TOKEN_HOURS} hours.",
		)
	except MailError as exc:
		logger.error("confirmation email to %s failed: %s", email, exc)
		raise OperationError(
			"mail_failed", "could not send the confirmation email; try again later"
		) from exc
	return RegisteredUser(
		user_id=user.id, username=user.login, email=email, confirmation_required=True
	)


async def confirm_email(registry: AsyncEngine, token: str) -> bool:
	"""Confirm an email by its token; False when the token is unknown/expired."""
	record = await registry_ops.confirm_email_by_token_hash(
		registry, _sha256(token), now=_now()
	)
	return record is not None


async def sign_in(
	registry: AsyncEngine, *, identifier: str, password: str, remember: bool
) -> SignedIn:
	"""Sign in by username or email; issues a session with a CSRF token.

	Raises:
		OperationError: invalid_credentials / email_not_confirmed.
	"""
	await registry_ops.delete_expired_sessions(registry, now=_now())

	identifier = identifier.strip()
	user = await registry_ops.get_user_by_login(registry, identifier)
	if user is None:
		email = await registry_ops.get_email_by_address(registry, identifier.lower())
		if email is not None:
			user = await registry_ops.get_user_by_id(registry, email.user_id)
	if user is None or user.status != "active":
		# Одинаковый ответ для «нет пользователя» и «не тот пароль».
		raise OperationError("invalid_credentials", "wrong username/email or password")
	try:
		_hasher.verify(user.password_hash, password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		raise OperationError(
			"invalid_credentials", "wrong username/email or password"
		) from exc

	emails = await registry_ops.list_user_emails(registry, user.id)
	primary = next((e for e in emails if e.is_primary), None)
	if primary is not None and primary.confirmed_at is None:
		raise OperationError(
			"email_not_confirmed", "confirm your email first (check your inbox)"
		)

	token = secrets.token_urlsafe(32)
	csrf = secrets.token_urlsafe(32)
	days = SESSION_DAYS_REMEMBER if remember else SESSION_DAYS
	expires = _now() + timedelta(days=days)
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
		registry, _sha256(session_token), now=_now()
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
) -> bool:
	"""Attach a secondary email; returns True when confirmation is required.

	Raises:
		OperationError: invalid_email / email_taken / mail_failed.
	"""
	address = address.strip().lower()
	if not _EMAIL_RE.fullmatch(address):
		raise OperationError("invalid_email", "email address looks invalid")
	if await registry_ops.get_email_by_address(registry, address) is not None:
		raise OperationError("email_taken", "this email is already attached to an account")

	if mailer is None:
		logger.warning("mail sender is not configured; email %s auto-confirmed", address)
		await registry_ops.add_user_email(
			registry,
			user_id=user_id,
			address=address,
			is_primary=False,
			confirm_token_hash=None,
			confirm_expires_at=None,
			confirmed_at=_now(),
		)
		return False

	token = secrets.token_urlsafe(32)
	await registry_ops.add_user_email(
		registry,
		user_id=user_id,
		address=address,
		is_primary=False,
		confirm_token_hash=_sha256(token),
		confirm_expires_at=_now() + timedelta(hours=CONFIRM_TOKEN_HOURS),
	)
	link = f"{confirm_base_url.rstrip('/')}/v1/user/confirm-email?token={token}"
	try:
		await mailer.send(
			address,
			"SeedRays: confirm your email",
			"Follow the link to confirm this email address:\n"
			f"{link}\n\nThe link is valid for {CONFIRM_TOKEN_HOURS} hours.",
		)
	except MailError as exc:
		logger.error("confirmation email to %s failed: %s", address, exc)
		raise OperationError(
			"mail_failed", "could not send the confirmation email; try again later"
		) from exc
	return True


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
