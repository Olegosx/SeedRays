"""Registry database operations: users, assets, settings, watcher state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import user_db_path
from seedrays.storage.migrations.runner import upgrade_user_db
from seedrays.storage.schema_registry import assets, settings, users, watcher_state


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
