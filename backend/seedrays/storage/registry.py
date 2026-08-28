"""Registry database operations: the first slice (users)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import user_db_path
from seedrays.storage.migrations.runner import upgrade_user_db
from seedrays.storage.schema_registry import users


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
