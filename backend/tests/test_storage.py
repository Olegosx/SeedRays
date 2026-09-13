"""Storage layer tests: migrations, constraints and the first registry operations."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_user
from seedrays.storage import user_store
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_all, upgrade_registry, upgrade_user_db

REGISTRY_TABLES = {
	"users",
	"operators",
	"api_keys",
	"wallet_xpubs",
	"settings",
	"assets",
	"watcher_state",
	"user_emails",
	"sessions",
	"password_resets",
	"operator_sessions",
}
USER_TABLES = {
	"wallets",
	"applications",
	"app_networks",
	"app_users",
	"bindings",
	"balances",
	"transactions",
	"mempool_queue",
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


def test_app_user_identity_spans_instances(tmp_path: Path) -> None:
	"""One external id lives in many instances, but only once per instance (ADR-0025)."""

	async def scenario() -> None:
		db_path = tmp_path / "user.db"
		upgrade_user_db(db_path)
		engine = create_sqlite_engine(db_path)
		async with engine.begin() as conn:
			await conn.execute(
				insert(schema_user.applications).values(name="shop", key_hash="k1")
			)
			# Экземпляр по умолчанию — пустая строка из server_default:
			# приложение с единственной установкой о колонке не знает.
			await conn.execute(
				insert(schema_user.app_users).values(application_id=1, external_id="42")
			)
			# Тот же идентификатор в другой установке — другой человек.
			await conn.execute(
				insert(schema_user.app_users).values(
					application_id=1, instance="munich", external_id="42"
				)
			)

		# А вот дубль внутри одной установки по-прежнему невозможен.
		with pytest.raises(IntegrityError):
			async with engine.begin() as conn:
				await conn.execute(
					insert(schema_user.app_users).values(
						application_id=1, instance="munich", external_id="42"
					)
				)

		async with engine.connect() as conn:
			rows = (await conn.execute(select(schema_user.app_users))).all()
		assert sorted(row.instance for row in rows) == ["", "munich"]
		await engine.dispose()

	asyncio.run(scenario())


def test_registry_settings_assets_and_watcher_state(tmp_path: Path) -> None:
	"""Settings roundtrip, asset auto-catalog idempotency and the 0002 cursor column."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		engine = create_sqlite_engine(registry_db_path(tmp_path))

		# настройки: чтение пустого, запись, перезапись
		assert await registry_ops.get_setting(engine, "k") is None
		await registry_ops.set_setting(engine, "k", "v1")
		await registry_ops.set_setting(engine, "k", "v2")
		assert await registry_ops.get_setting(engine, "k") == "v2"

		# активы: найти-или-создать не плодит дублей
		first = await registry_ops.get_or_create_asset(
			engine, network="tron", kind="token", contract_address="C1", symbol="USDT", decimals=6
		)
		second = await registry_ops.get_or_create_asset(
			engine, network="tron", kind="token", contract_address="C1", symbol="USDT", decimals=6
		)
		assert first.id == second.id
		assert len(await registry_ops.list_assets(engine, "tron")) == 1

		# состояние watcher: миграция 0002 добавила курсор времени
		assert await registry_ops.get_watcher_state(engine, "tron") is None
		moment = datetime(2026, 8, 29, 12, 0)
		await registry_ops.set_watcher_state(engine, "tron", last_block=10, last_scan_at=moment)
		await registry_ops.set_watcher_state(engine, "tron", last_block=20, last_scan_at=moment)
		state = await registry_ops.get_watcher_state(engine, "tron")
		assert state is not None
		assert state.last_block == 20
		assert state.last_scan_at == moment
		await engine.dispose()

	asyncio.run(scenario())


def test_record_transaction_rejects_invalid_domain_values(tmp_path: Path) -> None:
	"""A bad direction/status raises instead of masquerading as "already seen"."""

	async def scenario() -> None:
		from seedrays.storage import user_store
		from seedrays.storage.migrations.runner import upgrade_user_db

		db_path = user_db_path(tmp_path, "u1")
		db_path.parent.mkdir(parents=True)
		upgrade_user_db(db_path)
		engine = create_sqlite_engine(db_path)
		try:
			with pytest.raises(ValueError):
				await user_store.record_transaction(
					engine,
					address="T-addr",
					txid="tx1",
					asset_id=1,
					direction="sideways",
					amount=1,
					block_number=1,
					tx_time=None,
					status="success",
				)
			with pytest.raises(ValueError):
				await user_store.record_transaction(
					engine,
					address="T-addr",
					txid="tx1",
					asset_id=1,
					direction="in",
					amount=1,
					block_number=1,
					tx_time=None,
					status="maybe",
				)
		finally:
			await engine.dispose()

	asyncio.run(scenario())


def test_a_user_id_is_never_handed_out_twice(tmp_path: Path) -> None:
	"""Deleting the last user must not free their id for the next one.

	Тот же номер опознаёт человека в базе биллинга и даёт имя каталогу с его
	собственной базой. Пока номер выдавался заново, следующий
	зарегистрировавшийся получал вместе с ним чужую приостановку доступа,
	чужой платёжный адрес и чужой счёт (ADR-0024, ADR-0027).
	"""

	async def scenario() -> tuple[int, int, str]:
		upgrade_registry(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			await registry_ops.create_user(registry, tmp_path, "alice", "hash")
			bob = await registry_ops.create_user(registry, tmp_path, "bob", "hash")
			await registry_ops.delete_user_bundle(registry, bob.id)
			carol = await registry_ops.create_user(registry, tmp_path, "carol", "hash")
			return bob.id, carol.id, carol.directory
		finally:
			await registry.dispose()

	bob_id, carol_id, carol_dir = asyncio.run(scenario())
	assert carol_id != bob_id, "номер удалённого пользователя достался новому"
	assert carol_dir == f"u{carol_id}", "каталог назван по собственному номеру"


def test_history_read_returns_a_page_not_the_whole_table(tmp_path: Path) -> None:
	"""The page size and the status filter are part of the query, not postprocessing.

	История растёт годами, а страница дашборда — пять строк: пока отбор шёл
	перебором уже вычитанных строк, каждое открытие кабинета материализовало
	всю таблицу и занимало единственный процесс шлюза (ADR-0003).
	"""

	async def scenario() -> tuple[int, int, list[str]]:
		from seedrays.storage import user_views

		db_path = user_db_path(tmp_path, "u1")
		db_path.parent.mkdir(parents=True, exist_ok=True)
		upgrade_user_db(db_path)
		engine = create_sqlite_engine(db_path)
		try:
			for i in range(50):
				await user_store.record_transaction(
					engine,
					address="TAddr",
					txid=f"tx{i}",
					asset_id=1,
					direction="in",
					amount=1_000,
					block_number=i,
					tx_time=None,
					status="success",
					# Половина учтена в балансе, половина ещё нет.
					finalized_at=datetime(2026, 9, 1) if i % 2 == 0 else None,
				)
			await user_store.apply_finalized(
				engine, asset_ids={1}, applied_at=datetime(2026, 9, 1)
			)
			page = await user_views.list_incoming(engine, limit=5)
			confirmed = await user_views.list_incoming(engine, status="confirmed")
			pending = await user_views.list_incoming(engine, status="pending", limit=3)
			return len(page), len(confirmed), [r.txid for r in pending]
		finally:
			await engine.dispose()

	page, confirmed, pending = asyncio.run(scenario())
	assert page == 5, "запрос отдал больше страницы"
	assert confirmed == 25, "отбор по статусу не выполнен запросом"
	assert len(pending) == 3
