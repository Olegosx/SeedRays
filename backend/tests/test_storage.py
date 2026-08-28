"""Storage layer tests: migrations, constraints and the first registry operations."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import insert, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_user
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_all, upgrade_registry, upgrade_user_db

REGISTRY_TABLES = {"users", "operators", "api_keys", "settings", "assets", "watcher_state"}
USER_TABLES = {
	"wallets",
	"applications",
	"app_networks",
	"app_users",
	"bindings",
	"balances",
	"tx_pending",
	"tx_confirmed",
	"tx_failed",
}


async def _table_names(engine: AsyncEngine) -> set[str]:
	"""Table names of a database, minus the Alembic bookkeeping table."""
	async with engine.connect() as conn:
		names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
	await engine.dispose()
	return set(names) - {"alembic_version"}


def test_upgrade_registry_creates_schema(tmp_path: Path) -> None:
	"""The registry migration creates every registry table from scratch."""
	upgrade_registry(tmp_path)
	names = asyncio.run(_table_names(create_sqlite_engine(registry_db_path(tmp_path))))
	assert names == REGISTRY_TABLES


def test_upgrade_user_db_creates_schema(tmp_path: Path) -> None:
	"""The user-stream migration creates every user table from scratch."""
	db_path = tmp_path / "user.db"
	upgrade_user_db(db_path)
	names = asyncio.run(_table_names(create_sqlite_engine(db_path)))
	assert names == USER_TABLES


def test_upgrade_all_covers_existing_user_dbs(tmp_path: Path) -> None:
	"""upgrade_all migrates the registry and loops over user databases found on disk."""
	db_path = user_db_path(tmp_path, "u1")
	db_path.parent.mkdir(parents=True)
	db_path.touch()
	upgrade_all(tmp_path)
	assert registry_db_path(tmp_path).exists()
	names = asyncio.run(_table_names(create_sqlite_engine(db_path)))
	assert names == USER_TABLES


def test_create_user_and_lookup(tmp_path: Path) -> None:
	"""create_user writes the registry row and prepares the user's own database."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		engine = create_sqlite_engine(registry_db_path(tmp_path))
		record = await registry_ops.create_user(engine, tmp_path, "alice", "hash")
		assert record.login == "alice"
		assert record.status == "active"
		assert record.directory == f"u{record.id}"
		assert user_db_path(tmp_path, record.directory).exists()

		found = await registry_ops.get_user_by_login(engine, "alice")
		assert found == record
		assert await registry_ops.get_user_by_login(engine, "nobody") is None
		await engine.dispose()

	asyncio.run(scenario())


def test_create_user_duplicate_login(tmp_path: Path) -> None:
	"""A duplicate login is rejected with a clear error."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		engine = create_sqlite_engine(registry_db_path(tmp_path))
		await registry_ops.create_user(engine, tmp_path, "alice", "hash")
		with pytest.raises(ValueError, match="already taken"):
			await registry_ops.create_user(engine, tmp_path, "alice", "hash")
		await engine.dispose()

	asyncio.run(scenario())


def _binding_values(app_user_id: int, application_id: int, address: str) -> dict:
	"""Common binding row for the constraint tests."""
	return {
		"wallet_id": 1,
		"network": "tron",
		"address": address,
		"application_id": application_id,
		"app_user_id": app_user_id,
		"derivation_index": 0,
	}


def test_binding_constraints(tmp_path: Path) -> None:
	"""Uniqueness and the composite application/app-user FK hold at the database level."""

	async def scenario() -> None:
		db_path = tmp_path / "user.db"
		upgrade_user_db(db_path)
		engine = create_sqlite_engine(db_path)
		async with engine.begin() as conn:
			await conn.execute(insert(schema_user.wallets).values(family="tron", xpub="xpub"))
			await conn.execute(
				insert(schema_user.applications).values(name="shop", key_hash="k1")
			)
			await conn.execute(
				insert(schema_user.applications).values(name="blog", key_hash="k2")
			)
			await conn.execute(
				insert(schema_user.app_users).values(application_id=1, external_id="user1")
			)
			await conn.execute(
				insert(schema_user.bindings).values(_binding_values(1, 1, "Taddr1"))
			)

		# Повтор привязки для той же четвёрки владельца — нарушение uq_bindings_owner.
		with pytest.raises(IntegrityError):
			async with engine.begin() as conn:
				await conn.execute(
					insert(schema_user.bindings).values(_binding_values(1, 1, "Taddr2"))
				)

		# Пользователь приложения 1, а привязка ссылается на приложение 2 —
		# составной внешний ключ обязан это отвергнуть.
		with pytest.raises(IntegrityError):
			async with engine.begin() as conn:
				await conn.execute(
					insert(schema_user.bindings).values(_binding_values(1, 2, "Taddr3"))
				)
		await engine.dispose()

	asyncio.run(scenario())
