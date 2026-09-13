"""Registry database operations.

Users, emails, sessions, the API-key and wallet-xpub indexes, the asset
catalog, gateway settings and the watcher service state. Every datetime
parameter and column follows the storage-layer convention: naive UTC
(see :func:`seedrays.storage.engine.now_utc`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import unique_violation, upsert, user_db_path, user_dir_name
from seedrays.storage.migrations.runner import upgrade_user_db
from seedrays.storage.schema_registry import (
	api_keys,
	assets,
	operator_sessions,
	operators,
	password_resets,
	sessions,
	settings,
	user_emails,
	users,
	wallet_xpubs,
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
	created_at: datetime | None = None


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
			if unique_violation(exc) != "users.login":
				raise  # иная ошибка целостности — не «логин занят»
			raise ValueError(f"login already taken: {login!r}") from exc
		user_id = result.inserted_primary_key[0]
		directory = user_dir_name(user_id)
		await conn.execute(update(users).where(users.c.id == user_id).values(directory=directory))

	db_path = user_db_path(data_dir, directory)
	try:
		db_path.parent.mkdir(parents=True, exist_ok=True)
		# Alembic работает синхронно — уводим миграцию новой базы в поток.
		await asyncio.to_thread(upgrade_user_db, db_path)
	except BaseException:
		# Компенсация: без своей базы пользователь неработоспособен, а его
		# строка навсегда занимала бы логин — снимаем её и поднимаем ошибку.
		async with registry.begin() as conn:
			await conn.execute(delete(users).where(users.c.id == user_id))
		raise

	record = await get_user_by_login(registry, login)
	if record is None:
		raise RuntimeError(f"user {login!r} not found right after insert")
	return record


def _user_record(row) -> UserRecord:
	"""Build a UserRecord out of a row."""
	return UserRecord(
		id=row.id,
		login=row.login,
		password_hash=row.password_hash,
		status=row.status,
		directory=row.directory,
		created_at=row.created_at,
	)


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
	return _user_record(row)


async def get_user_by_id(registry: AsyncEngine, user_id: int) -> UserRecord | None:
	"""Fetch a user by id; None if unknown."""
	async with registry.connect() as conn:
		row = (await conn.execute(select(users).where(users.c.id == user_id))).first()
	if row is None:
		return None
	return _user_record(row)


async def delete_user_record(registry: AsyncEngine, user_id: int) -> None:
	"""Remove a user row with their email rows (compensation of a failed registration)."""
	async with registry.begin() as conn:
		await conn.execute(delete(user_emails).where(user_emails.c.user_id == user_id))
		await conn.execute(delete(users).where(users.c.id == user_id))


def _dt_str(value: datetime | None) -> str | None:
	"""Serialize a storage datetime for the user-deletion archive."""
	return None if value is None else value.isoformat(sep=" ")


def _dt_parse(value: str | None) -> datetime | None:
	"""Parse a datetime back from the user-deletion archive."""
	return None if value is None else datetime.fromisoformat(value)


async def read_user_bundle(registry: AsyncEngine, user_id: int) -> dict | None:
	"""Snapshot every registry row of one user (the user-deletion archive).

	Sessions and password-reset tokens are deliberately not part of the
	bundle: they are dropped on deletion and a restored user simply signs
	in again.

	Args:
		registry: Engine of the shared registry database.
		user_id: The user being archived.

	Returns:
		A JSON-serializable dict of the user's rows, or None for an
		unknown user.
	"""
	async with registry.connect() as conn:
		user_row = (await conn.execute(select(users).where(users.c.id == user_id))).first()
		if user_row is None:
			return None
		email_rows = (
			await conn.execute(select(user_emails).where(user_emails.c.user_id == user_id))
		).all()
		xpub_rows = (
			await conn.execute(select(wallet_xpubs).where(wallet_xpubs.c.user_id == user_id))
		).all()
		key_rows = (
			await conn.execute(select(api_keys).where(api_keys.c.user_id == user_id))
		).all()
	return {
		"user": {
			"id": user_row.id,
			"login": user_row.login,
			"password_hash": user_row.password_hash,
			"status": user_row.status,
			"directory": user_row.directory,
			"created_at": _dt_str(user_row.created_at),
		},
		"emails": [
			{
				"id": row.id,
				"address": row.address,
				"is_primary": row.is_primary,
				"confirmed_at": _dt_str(row.confirmed_at),
				"confirm_token_hash": row.confirm_token_hash,
				"confirm_expires_at": _dt_str(row.confirm_expires_at),
				"created_at": _dt_str(row.created_at),
			}
			for row in email_rows
		],
		"wallet_xpubs": [
			{"id": row.id, "xpub_hash": row.xpub_hash, "created_at": _dt_str(row.created_at)}
			for row in xpub_rows
		],
		"api_keys": [
			{"id": row.id, "key_hash": row.key_hash, "created_at": _dt_str(row.created_at)}
			for row in key_rows
		],
	}


async def delete_user_bundle(registry: AsyncEngine, user_id: int) -> None:
	"""Drop every registry row of one user in one transaction.

	Дочерние строки удаляются раньше строки ``users`` — внешние ключи
	включены (PRAGMA foreign_keys=ON), иначе удаление отвергнет база.
	"""
	async with registry.begin() as conn:
		await conn.execute(delete(password_resets).where(password_resets.c.user_id == user_id))
		await conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
		await conn.execute(delete(api_keys).where(api_keys.c.user_id == user_id))
		await conn.execute(delete(wallet_xpubs).where(wallet_xpubs.c.user_id == user_id))
		await conn.execute(delete(user_emails).where(user_emails.c.user_id == user_id))
		await conn.execute(delete(users).where(users.c.id == user_id))


async def restore_user_bundle(registry: AsyncEngine, bundle: dict) -> None:
	"""Re-insert the archived registry rows of one user (all or nothing).

	Args:
		registry: Engine of the shared registry database.
		bundle: The dict produced by :func:`read_user_bundle`.

	Raises:
		ValueError: Naming the conflicting unique columns when the user id,
			login, an email, an xpub or an API key has been taken since the
			deletion.
	"""
	user = bundle["user"]
	try:
		async with registry.begin() as conn:
			await conn.execute(
				insert(users).values(
					id=user["id"],
					login=user["login"],
					password_hash=user["password_hash"],
					status=user["status"],
					directory=user["directory"],
					created_at=_dt_parse(user["created_at"]),
				)
			)
			for row in bundle["emails"]:
				await conn.execute(
					insert(user_emails).values(
						id=row["id"],
						user_id=user["id"],
						address=row["address"],
						is_primary=row["is_primary"],
						confirmed_at=_dt_parse(row["confirmed_at"]),
						confirm_token_hash=row["confirm_token_hash"],
						confirm_expires_at=_dt_parse(row["confirm_expires_at"]),
						created_at=_dt_parse(row["created_at"]),
					)
				)
			for row in bundle["wallet_xpubs"]:
				await conn.execute(
					insert(wallet_xpubs).values(
						id=row["id"],
						user_id=user["id"],
						xpub_hash=row["xpub_hash"],
						created_at=_dt_parse(row["created_at"]),
					)
				)
			for row in bundle["api_keys"]:
				await conn.execute(
					insert(api_keys).values(
						id=row["id"],
						user_id=user["id"],
						key_hash=row["key_hash"],
						created_at=_dt_parse(row["created_at"]),
					)
				)
	except IntegrityError as exc:
		taken = unique_violation(exc)
		if taken is None:
			raise  # иная ошибка целостности — не «занято с момента удаления»
		raise ValueError(f"already taken since the deletion: {taken}") from exc


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
		if unique_violation(exc) != "user_emails.address":
			raise  # иная ошибка целостности — не «адрес занят»
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


async def set_password_reset(
	registry: AsyncEngine, *, user_id: int, token_hash: str, expires_at: datetime
) -> None:
	"""Store the user's password-reset token; a new request replaces the old one."""
	async with registry.begin() as conn:
		await upsert(
			conn,
			password_resets,
			{"user_id": user_id},
			{"token_hash": token_hash, "expires_at": expires_at},
		)


async def consume_password_reset(
	registry: AsyncEngine, token_hash: str, *, now: datetime
) -> int | None:
	"""Use up an unexpired reset token: returns its user id and drops the row.

	Токен одноразовый: чтение и удаление — в одной транзакции, повторное
	предъявление того же токена ничего не находит.
	"""
	async with registry.begin() as conn:
		row = (
			await conn.execute(
				select(password_resets).where(
					password_resets.c.token_hash == token_hash,
					password_resets.c.expires_at >= now,
				)
			)
		).first()
		if row is None:
			return None
		await conn.execute(
			delete(password_resets).where(password_resets.c.id == row.id)
		)
	return row.user_id


@dataclass(frozen=True)
class OperatorRecord:
	"""An operator row from the registry."""

	id: int
	login: str
	password_hash: str
	status: str


def _operator_record(row) -> OperatorRecord:
	return OperatorRecord(
		id=row.id, login=row.login, password_hash=row.password_hash, status=row.status
	)


async def create_operator(registry: AsyncEngine, login: str, password_hash: str) -> int:
	"""Create an operator account; returns its id.

	Raises:
		ValueError: If the login is already taken.
	"""
	try:
		async with registry.begin() as conn:
			result = await conn.execute(
				insert(operators).values(login=login, password_hash=password_hash)
			)
	except IntegrityError as exc:
		if unique_violation(exc) != "operators.login":
			raise  # иная ошибка целостности — не «логин занят»
		raise ValueError(f"operator login already taken: {login!r}") from exc
	return result.inserted_primary_key[0]


async def get_operator_by_login(registry: AsyncEngine, login: str) -> OperatorRecord | None:
	"""Fetch an operator by login; None if unknown."""
	async with registry.connect() as conn:
		row = (await conn.execute(select(operators).where(operators.c.login == login))).first()
	return None if row is None else _operator_record(row)


async def get_operator_by_id(registry: AsyncEngine, operator_id: int) -> OperatorRecord | None:
	"""Fetch an operator by id; None if unknown."""
	async with registry.connect() as conn:
		row = (
			await conn.execute(select(operators).where(operators.c.id == operator_id))
		).first()
	return None if row is None else _operator_record(row)


async def set_operator_password(
	registry: AsyncEngine, operator_id: int, password_hash: str
) -> None:
	"""Replace the operator's password hash."""
	async with registry.begin() as conn:
		await conn.execute(
			update(operators)
			.where(operators.c.id == operator_id)
			.values(password_hash=password_hash)
		)


async def create_operator_session(
	registry: AsyncEngine,
	*,
	operator_id: int,
	token_hash: str,
	csrf_token: str,
	expires_at: datetime,
) -> None:
	"""Store a new operator-panel session."""
	async with registry.begin() as conn:
		await conn.execute(
			insert(operator_sessions).values(
				operator_id=operator_id,
				token_hash=token_hash,
				csrf_token=csrf_token,
				expires_at=expires_at,
			)
		)


async def get_operator_session_by_token_hash(
	registry: AsyncEngine, token_hash: str, *, now: datetime
):
	"""Resolve an unexpired operator session row; None otherwise."""
	async with registry.connect() as conn:
		return (
			await conn.execute(
				select(operator_sessions).where(
					operator_sessions.c.token_hash == token_hash,
					operator_sessions.c.expires_at >= now,
				)
			)
		).first()


async def delete_operator_session(registry: AsyncEngine, token_hash: str) -> None:
	"""Drop one operator session (sign-out)."""
	async with registry.begin() as conn:
		await conn.execute(
			delete(operator_sessions).where(operator_sessions.c.token_hash == token_hash)
		)


async def delete_operator_sessions(
	registry: AsyncEngine, operator_id: int, *, keep_token_hash: str | None = None
) -> None:
	"""Drop the operator's sessions; optionally keep the current one."""
	query = delete(operator_sessions).where(
		operator_sessions.c.operator_id == operator_id
	)
	if keep_token_hash is not None:
		query = query.where(operator_sessions.c.token_hash != keep_token_hash)
	async with registry.begin() as conn:
		await conn.execute(query)


async def set_user_status(registry: AsyncEngine, user_id: int, status: str) -> None:
	"""Set a gateway user's status (active / blocked)."""
	async with registry.begin() as conn:
		await conn.execute(update(users).where(users.c.id == user_id).values(status=status))


async def list_emails_by_users(
	registry: AsyncEngine, user_ids: list[int]
) -> dict[int, list[EmailRecord]]:
	"""user id → their email rows (the operator's user list)."""
	if not user_ids:
		return {}
	async with registry.connect() as conn:
		rows = (
			await conn.execute(
				select(user_emails).where(user_emails.c.user_id.in_(user_ids))
			)
		).all()
	result: dict[int, list[EmailRecord]] = {}
	for row in rows:
		result.setdefault(row.user_id, []).append(_email_record(row))
	return result


# Виды активов каталога (ADR-0010) — единственная точка правды для сравнений.
KIND_NATIVE = "native"
KIND_TOKEN = "token"

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
	return [_user_record(row) for row in rows]


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
	except IntegrityError as exc:
		if unique_violation(exc) is None:
			raise  # иная ошибка целостности — не гонка вставки актива
		# Параллельная вставка того же актива — читаем существующий.
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


async def asset_ids_matching(
	registry: AsyncEngine,
	*,
	network: str | None = None,
	symbol: str | None = None,
	contract_address: str | None = None,
) -> set[int]:
	"""Ids of the catalog assets matching a network and/or a symbol.

	Нужна для того, чтобы отбор по сети и активу выполнялся запросом к базе
	владельца, а не перебором в Python: экранное чтение обязано возвращать
	страницу, а не всю историю (ADR-0006, дополнение о чтениях под экран).
	Активы лежат в общем реестре, а строки — в базе пользователя, поэтому
	фильтр сначала разрешается в набор идентификаторов.
	"""
	query = select(assets.c.id)
	if network is not None:
		query = query.where(assets.c.network == network)
	if symbol is not None:
		query = query.where(assets.c.symbol == symbol)
	if contract_address is not None:
		# Пустой адрес контракта — признак родной монеты сети (ADR-0010),
		# он же значение фильтра «native» в API приложений.
		query = query.where(assets.c.contract_address == contract_address)
	async with registry.connect() as conn:
		return {row.id for row in (await conn.execute(query)).all()}


async def get_setting(registry: AsyncEngine, key: str) -> str | None:
	"""Read one gateway setting; None if absent."""
	async with registry.connect() as conn:
		row = (await conn.execute(select(settings).where(settings.c.key == key))).first()
	return None if row is None else row.value


async def set_setting(registry: AsyncEngine, key: str, value: str) -> None:
	"""Create or replace one gateway setting."""
	async with registry.begin() as conn:
		await upsert(conn, settings, {"key": key}, {"value": value})


async def reserve_wallet_xpub(
	registry: AsyncEngine, *, user_id: int, xpub_hash: str
) -> bool:
	"""Reserve an xpub fingerprint in the gateway-wide index (one xpub — one wallet).

	Args:
		registry: Engine of the shared registry database.
		user_id: Owner of the wallet being attached.
		xpub_hash: SHA-256 fingerprint of the normalized xpub.

	Returns:
		True if reserved; False if the fingerprint is already taken.
	"""
	try:
		async with registry.begin() as conn:
			await conn.execute(
				insert(wallet_xpubs).values(xpub_hash=xpub_hash, user_id=user_id)
			)
	except IntegrityError as exc:
		if unique_violation(exc) != "wallet_xpubs.xpub_hash":
			raise  # иная ошибка целостности — не «xpub занят»
		return False
	return True


async def wallet_xpub_taken(registry: AsyncEngine, xpub_hash: str) -> bool:
	"""Whether a key fingerprint is already reserved by some user's wallet.

	A read without a reservation: the billing side checks the owner's master
	wallet against this index, and reserving there would be wrong — the
	master wallet belongs to no user and lives in the billing database
	(ADR-0027).
	"""
	async with registry.connect() as conn:
		row = (
			await conn.execute(
				select(wallet_xpubs.c.id).where(wallet_xpubs.c.xpub_hash == xpub_hash)
			)
		).first()
	return row is not None


async def release_wallet_xpub(registry: AsyncEngine, xpub_hash: str) -> None:
	"""Release a reserved xpub fingerprint (compensation for a failed attach)."""
	async with registry.begin() as conn:
		await conn.execute(delete(wallet_xpubs).where(wallet_xpubs.c.xpub_hash == xpub_hash))


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
		await upsert(
			conn,
			watcher_state,
			{"network": network},
			{"last_block": last_block, "last_scan_at": last_scan_at},
		)


async def get_assets_by_ids(
	registry: AsyncEngine, asset_ids: set[int]
) -> dict[int, AssetRecord]:
	"""Catalog assets by their ids — the shared lookup of every read path."""
	if not asset_ids:
		return {}
	async with registry.connect() as conn:
		rows = (await conn.execute(select(assets).where(assets.c.id.in_(asset_ids)))).all()
	return {row.id: _asset_record(row) for row in rows}


async def resolve_api_key(registry: AsyncEngine, key_hash: str) -> UserRecord | None:
	"""The owner of an application API key, or None for an unknown key (ADR-0008)."""
	async with registry.connect() as conn:
		key_row = (
			await conn.execute(select(api_keys).where(api_keys.c.key_hash == key_hash))
		).first()
	if key_row is None:
		return None
	return await get_user_by_id(registry, key_row.user_id)


async def add_api_key(registry: AsyncEngine, *, user_id: int, key_hash: str) -> None:
	"""Add one key fingerprint to the gateway-wide index."""
	async with registry.begin() as conn:
		await conn.execute(insert(api_keys).values(key_hash=key_hash, user_id=user_id))


async def delete_api_key(registry: AsyncEngine, key_hash: str) -> None:
	"""Drop one key fingerprint from the index (revocation, reissue)."""
	async with registry.begin() as conn:
		await conn.execute(delete(api_keys).where(api_keys.c.key_hash == key_hash))
