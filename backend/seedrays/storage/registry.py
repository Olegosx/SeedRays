"""Registry database operations: users, assets, settings, watcher state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import user_db_path
from seedrays.storage.migrations.runner import upgrade_user_db
from seedrays.storage.schema_registry import (
	assets,
	sessions,
	settings,
	user_emails,
	users,
	watcher_state,
)


@dataclass(frozen=True)
class UserRecord:
	"""A user row from the registry."""

	id: int
	login: str
	password_hash: str
	status: str
	directory: str


async def create_user(
	registry: AsyncEngine, data_dir: Path, login: str, password_hash: str
) -> UserRecord:
	"""Create a user: a registry row plus the user's directory with a migrated database.

	Args:
		registry: Engine of the shared registry database.
		data_dir: Gateway data directory.
		login: Unique login.
		password_hash: Argon2 hash of the user's password.

	Returns:
		The created user record.

	Raises:
		ValueError: If the login is already taken.
	"""
	async with registry.begin() as conn:
		try:
			result = await conn.execute(
				insert(users).values(login=login, password_hash=password_hash, directory="")
			)
		except IntegrityError as exc:
			raise ValueError(f"login already taken: {login!r}") from exc
		user_id = result.inserted_primary_key[0]
		directory = f"u{user_id}"
		await conn.execute(update(users).where(users.c.id == user_id).values(directory=directory))

	db_path = user_db_path(data_dir, directory)
	db_path.parent.mkdir(parents=True, exist_ok=True)
	# Alembic работает синхронно — уводим миграцию новой базы в поток.
	await asyncio.to_thread(upgrade_user_db, db_path)

	record = await get_user_by_login(registry, login)
	if record is None:
		raise RuntimeError(f"user {login!r} not found right after insert")
	return record


async def get_user_by_login(registry: AsyncEngine, login: str) -> UserRecord | None:
	"""Fetch a user by login.

	Args:
		registry: Engine of the shared registry database.
		login: Login to look up.

	Returns:
		The user record, or None if the login is unknown.
	"""
	async with registry.connect() as conn:
		row = (await conn.execute(select(users).where(users.c.login == login))).first()
	if row is None:
		return None
	return UserRecord(
		id=row.id,
		login=row.login,
		password_hash=row.password_hash,
		status=row.status,
		directory=row.directory,
	)


async def get_user_by_id(registry: AsyncEngine, user_id: int) -> UserRecord | None:
	"""Fetch a user by id; None if unknown."""
	async with registry.connect() as conn:
		row = (await conn.execute(select(users).where(users.c.id == user_id))).first()
	if row is None:
		return None
	return UserRecord(
		id=row.id,
		login=row.login,
		password_hash=row.password_hash,
		status=row.status,
		directory=row.directory,
	)


async def set_user_password(registry: AsyncEngine, user_id: int, password_hash: str) -> None:
	"""Replace the user's password hash."""
	async with registry.begin() as conn:
		await conn.execute(
			update(users).where(users.c.id == user_id).values(password_hash=password_hash)
		)


@dataclass(frozen=True)
class EmailRecord:
	"""One email address of a user."""

	id: int
	user_id: int
	address: str
	is_primary: bool
	confirmed_at: datetime | None


def _email_record(row) -> EmailRecord:
	return EmailRecord(
		id=row.id,
		user_id=row.user_id,
		address=row.address,
		is_primary=bool(row.is_primary),
		confirmed_at=row.confirmed_at,
	)


async def add_user_email(
	registry: AsyncEngine,
	*,
	user_id: int,
	address: str,
	is_primary: bool,
	confirm_token_hash: str | None,
	confirm_expires_at: datetime | None,
	confirmed_at: datetime | None = None,
) -> EmailRecord:
	"""Attach an email address to a user.

	Raises:
		ValueError: If the address is already attached to some account.
	"""
	try:
		async with registry.begin() as conn:
			result = await conn.execute(
				insert(user_emails).values(
					user_id=user_id,
					address=address,
					is_primary=int(is_primary),
					confirmed_at=confirmed_at,
					confirm_token_hash=confirm_token_hash,
					confirm_expires_at=confirm_expires_at,
				)
			)
	except IntegrityError as exc:
		raise ValueError(f"email already attached: {address!r}") from exc
	email_id = result.inserted_primary_key[0]
	async with registry.connect() as conn:
		row = (await conn.execute(select(user_emails).where(user_emails.c.id == email_id))).first()
	if row is None:
		raise RuntimeError(f"email {address!r} vanished after insert")
	return _email_record(row)


async def list_user_emails(registry: AsyncEngine, user_id: int) -> list[EmailRecord]:
	"""Every email address of one user, primary first."""
	async with registry.connect() as conn:
		rows = (
			await conn.execute(
				select(user_emails)
				.where(user_emails.c.user_id == user_id)
				.order_by(user_emails.c.is_primary.desc(), user_emails.c.id)
			)
		).all()
	return [_email_record(row) for row in rows]


async def get_email_by_address(registry: AsyncEngine, address: str) -> EmailRecord | None:
	"""Find an email row by address; None if unknown."""
	async with registry.connect() as conn:
		row = (
			await conn.execute(select(user_emails).where(user_emails.c.address == address))
		).first()
	return None if row is None else _email_record(row)


async def confirm_email_by_token_hash(
	registry: AsyncEngine, token_hash: str, *, now: datetime
) -> EmailRecord | None:
	"""Confirm the email matching an unexpired token hash; None if no match."""
	async with registry.begin() as conn:
		row = (
			await conn.execute(
				select(user_emails).where(
					user_emails.c.confirm_token_hash == token_hash,
					user_emails.c.confirm_expires_at.is_not(None),
					user_emails.c.confirm_expires_at >= now,
				)
			)
		).first()
		if row is None:
			return None
		await conn.execute(
			update(user_emails)
			.where(user_emails.c.id == row.id)
			.values(confirmed_at=now, confirm_token_hash=None, confirm_expires_at=None)
		)
	refreshed = await get_email_by_address(registry, row.address)
	return refreshed


@dataclass(frozen=True)
class SessionRecord:
	"""A cabinet session resolved from its cookie token."""

	id: int
	user_id: int
	csrf_token: str
	expires_at: datetime


async def create_session(
	registry: AsyncEngine,
	*,
	user_id: int,
	token_hash: str,
	csrf_token: str,
	expires_at: datetime,
) -> None:
	"""Store a new cabinet session."""
	async with registry.begin() as conn:
		await conn.execute(
			insert(sessions).values(
				user_id=user_id,
				token_hash=token_hash,
				csrf_token=csrf_token,
				expires_at=expires_at,
			)
		)


async def get_session_by_token_hash(
	registry: AsyncEngine, token_hash: str, *, now: datetime
) -> SessionRecord | None:
	"""Resolve an unexpired session by its token hash; None otherwise."""
	async with registry.connect() as conn:
		row = (
			await conn.execute(
				select(sessions).where(
					sessions.c.token_hash == token_hash, sessions.c.expires_at >= now
				)
			)
		).first()
	if row is None:
		return None
	return SessionRecord(
		id=row.id, user_id=row.user_id, csrf_token=row.csrf_token, expires_at=row.expires_at
	)


async def delete_session(registry: AsyncEngine, token_hash: str) -> None:
	"""Drop one session (sign-out)."""
	async with registry.begin() as conn:
		await conn.execute(delete(sessions).where(sessions.c.token_hash == token_hash))


async def delete_user_sessions(
	registry: AsyncEngine, user_id: int, *, keep_token_hash: str | None = None
) -> None:
	"""Drop the user's sessions; optionally keep the current one."""
	query = delete(sessions).where(sessions.c.user_id == user_id)
	if keep_token_hash is not None:
		query = query.where(sessions.c.token_hash != keep_token_hash)
	async with registry.begin() as conn:
		await conn.execute(query)


async def get_email_by_id(registry: AsyncEngine, email_id: int) -> EmailRecord | None:
	"""Find an email row by id; None if unknown."""
	async with registry.connect() as conn:
		row = (
			await conn.execute(select(user_emails).where(user_emails.c.id == email_id))
		).first()
	return None if row is None else _email_record(row)


async def delete_user_email(registry: AsyncEngine, email_id: int) -> None:
	"""Drop one email row."""
	async with registry.begin() as conn:
		await conn.execute(delete(user_emails).where(user_emails.c.id == email_id))


async def delete_expired_sessions(registry: AsyncEngine, *, now: datetime) -> None:
	"""Hygiene: drop every expired session."""
	async with registry.begin() as conn:
		await conn.execute(delete(sessions).where(sessions.c.expires_at < now))


@dataclass(frozen=True)
class AssetRecord:
	"""An asset row from the registry catalog."""

	id: int
	network: str
	kind: str
	contract_address: str
	symbol: str
	decimals: int


@dataclass(frozen=True)
class WatcherState:
	"""Per-network watcher service state."""

	network: str
	last_block: int
	last_scan_at: datetime | None


async def list_users(registry: AsyncEngine) -> list[UserRecord]:
	"""Return every user of the gateway."""
	async with registry.connect() as conn:
		rows = (await conn.execute(select(users))).all()
	return [
		UserRecord(
			id=row.id,
			login=row.login,
			password_hash=row.password_hash,
			status=row.status,
			directory=row.directory,
		)
		for row in rows
	]


def _asset_record(row) -> AssetRecord:
	"""Build an AssetRecord out of a row."""
	return AssetRecord(
		id=row.id,
		network=row.network,
		kind=row.kind,
		contract_address=row.contract_address,
		symbol=row.symbol,
		decimals=row.decimals,
	)


async def get_or_create_asset(
	registry: AsyncEngine,
	*,
	network: str,
	kind: str,
	contract_address: str,
	symbol: str,
	decimals: int,
) -> AssetRecord:
	"""Find an asset by (network, contract) or create it (auto-catalog, ADR-0010).

	Args:
		registry: Engine of the shared registry database.
		network: Network code the asset lives in.
		kind: ``native`` or ``token``.
		contract_address: Token contract; empty string for the native coin.
		symbol: Display symbol.
		decimals: Decimal places of the minimal unit.

	Returns:
		The existing or newly created asset record.
	"""
	lookup = select(assets).where(
		assets.c.network == network, assets.c.contract_address == contract_address
	)
	async with registry.connect() as conn:
		row = (await conn.execute(lookup)).first()
	if row is not None:
		return _asset_record(row)
	try:
		async with registry.begin() as conn:
			await conn.execute(
				insert(assets).values(
					network=network,
					kind=kind,
					contract_address=contract_address,
					symbol=symbol,
					decimals=decimals,
				)
			)
	except IntegrityError:
		pass  # параллельная вставка того же актива — читаем существующий
	async with registry.connect() as conn:
		row = (await conn.execute(lookup)).first()
	if row is None:
		raise RuntimeError(f"asset {network}/{contract_address!r} vanished after insert")
	return _asset_record(row)


async def list_assets(registry: AsyncEngine, network: str) -> list[AssetRecord]:
	"""Return every catalog asset of one network."""
	async with registry.connect() as conn:
		rows = (await conn.execute(select(assets).where(assets.c.network == network))).all()
	return [_asset_record(row) for row in rows]


async def get_setting(registry: AsyncEngine, key: str) -> str | None:
	"""Read one gateway setting; None if absent."""
	async with registry.connect() as conn:
		row = (await conn.execute(select(settings).where(settings.c.key == key))).first()
	return None if row is None else row.value


async def set_setting(registry: AsyncEngine, key: str, value: str) -> None:
	"""Create or replace one gateway setting."""
	async with registry.begin() as conn:
		result = await conn.execute(
			update(settings).where(settings.c.key == key).values(value=value)
		)
		if result.rowcount == 0:
			await conn.execute(insert(settings).values(key=key, value=value))


async def get_watcher_state(registry: AsyncEngine, network: str) -> WatcherState | None:
	"""Read the watcher service state of one network; None before the first pass."""
	async with registry.connect() as conn:
		row = (
			await conn.execute(select(watcher_state).where(watcher_state.c.network == network))
		).first()
	if row is None:
		return None
	return WatcherState(network=row.network, last_block=row.last_block, last_scan_at=row.last_scan_at)


async def set_watcher_state(
	registry: AsyncEngine, network: str, *, last_block: int, last_scan_at: datetime
) -> None:
	"""Create or replace the watcher service state of one network."""
	async with registry.begin() as conn:
		result = await conn.execute(
			update(watcher_state)
			.where(watcher_state.c.network == network)
			.values(last_block=last_block, last_scan_at=last_scan_at)
		)
		if result.rowcount == 0:
			await conn.execute(
				insert(watcher_state).values(
					network=network, last_block=last_block, last_scan_at=last_scan_at
				)
			)
