"""Billing tests: the schema stream and the turnover calculation (ADR-0027)."""

import asyncio
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.orchestrator import billing
from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_user
from seedrays.storage.engine import (
	billing_db_path,
	create_sqlite_engine,
	registry_db_path,
	user_db_path,
)
from seedrays.storage.migrations.runner import upgrade_all, upgrade_billing

BILLING_TABLES = {
	"master_wallets",
	"invoice_addresses",
	"invoices",
	"invoice_payments",
	"user_billing",
}

NETWORK = "tron-nile"
USDT = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
SPAM = "TSpamTokenContractAddress000000000"

PERIOD_START = datetime(2026, 9, 1)
PERIOD_END = datetime(2026, 10, 1)
INSIDE = datetime(2026, 9, 15, 12, 0)


async def _table_names(engine: AsyncEngine) -> set[str]:
	async with engine.connect() as conn:
		names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
	await engine.dispose()
	return set(names) - {"alembic_version"}


def test_upgrade_billing_creates_schema(tmp_path: Path) -> None:
	"""The billing migration creates every billing table from scratch."""
	upgrade_billing(tmp_path)
	names = asyncio.run(_table_names(create_sqlite_engine(billing_db_path(tmp_path))))
	assert names == BILLING_TABLES


def test_upgrade_all_covers_the_billing_database(tmp_path: Path) -> None:
	"""The gateway start-up migration covers the billing database too."""
	upgrade_all(tmp_path)
	assert billing_db_path(tmp_path).exists()


def test_period_bounds_is_a_calendar_month() -> None:
	"""Any moment of a month maps to that whole month, half-open."""
	start, end = billing.period_bounds(datetime(2026, 9, 15, 23, 59, 59))
	assert start == datetime(2026, 9, 1)
	assert end == datetime(2026, 10, 1)
	# Февраль високосного года: длина месяца берётся из календаря, не из 30.
	assert billing.period_bounds(datetime(2028, 2, 5)) == (
		datetime(2028, 2, 1),
		datetime(2028, 3, 1),
	)


def test_to_micro_usdt_scales_without_floats() -> None:
	"""Amounts are rescaled by decimals, and a finer asset loses its tail."""
	assert billing.to_micro_usdt(1_500_000, 6) == 1_500_000
	# 18 знаков (токен уровня BUSD): 1.5 монеты → 1.5 USDT.
	assert billing.to_micro_usdt(1_500_000_000_000_000_000, 18) == 1_500_000
	# Хвост тоньше микро-USDT отбрасывается, а не округляется вверх.
	assert billing.to_micro_usdt(1_999, 18) == 0
	# Актив грубее USDT (2 знака): 3.21 → 3.21 USDT.
	assert billing.to_micro_usdt(321, 2) == 3_210_000
	assert billing.format_usdt(1_500_000) == "1.5"
	assert billing.format_usdt(2_000_000) == "2"


async def _seed(tmp_path: Path) -> tuple[AsyncEngine, AsyncEngine, dict[str, int]]:
	"""A migrated gateway with one user and two catalog assets."""
	upgrade_all(tmp_path)
	registry = create_sqlite_engine(registry_db_path(tmp_path))
	user = await registry_ops.create_user(registry, tmp_path, "alice", "hash")
	usdt = await registry_ops.get_or_create_asset(
		registry,
		network=NETWORK,
		kind=registry_ops.KIND_TOKEN,
		contract_address=USDT,
		symbol="USDT",
		decimals=6,
	)
	spam = await registry_ops.get_or_create_asset(
		registry,
		network=NETWORK,
		kind=registry_ops.KIND_TOKEN,
		contract_address=SPAM,
		# Поддельный токен с тем же символом: в оборот попасть не должен.
		symbol="USDT",
		decimals=6,
	)
	await registry_ops.set_setting(
		registry, f"{billing.SETTING_ASSETS_PREFIX}{NETWORK}", f'["{USDT}"]'
	)
	engine = create_sqlite_engine(user_db_path(tmp_path, user.directory))
	return registry, engine, {"usdt": usdt.id, "spam": spam.id}


async def _record(
	engine: AsyncEngine,
	*,
	asset_id: int,
	amount: int,
	direction: str = "in",
	status: str = "success",
	finalized: bool = True,
	txid: str = "tx1",
	address: str = "TAddr1",
	tx_time: datetime | None = INSIDE,
	first_seen_at: datetime = INSIDE,
) -> None:
	"""Insert one raw transaction row into the user database.

	``first_seen_at`` is set explicitly: a row without a block time is dated
	by it, and letting the database default to "now" would make the test
	depend on the month it runs in.
	"""
	async with engine.begin() as conn:
		await conn.execute(
			insert(schema_user.transactions).values(
				address=address,
				txid=txid,
				asset_id=asset_id,
				direction=direction,
				amount=str(amount),
				block_number=100,
				tx_time=tx_time,
				first_seen_at=first_seen_at,
				status=status,
				finalized_at=INSIDE if finalized else None,
			)
		)


def test_turnover_counts_only_finalized_successful_incoming(tmp_path: Path) -> None:
	"""Provisional, failed and outgoing rows stay out of the turnover."""

	async def scenario() -> int:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _record(engine, asset_id=assets["usdt"], amount=1_000_000, txid="paid")
			await _record(
				engine, asset_id=assets["usdt"], amount=500_000, txid="pending",
				finalized=False,
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=700_000, txid="failed",
				status="failed",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=900_000, txid="sweep",
				direction="out",
			)
			return await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == 1_000_000


def test_turnover_ignores_assets_outside_the_operator_list(tmp_path: Path) -> None:
	"""A counterfeit token carrying the symbol "USDT" produces no turnover."""

	async def scenario() -> int:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _record(engine, asset_id=assets["spam"], amount=9_000_000, txid="spam")
			return await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == 0


def test_turnover_excludes_moves_between_own_addresses(tmp_path: Path) -> None:
	"""A transfer with an outgoing leg of the same user is not income."""

	async def scenario() -> int:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="move",
				direction="out", address="TAddrFrom",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="move",
				address="TAddrTo",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=3_000_000, txid="payment",
				address="TAddrTo",
			)
			return await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == 3_000_000


def test_turnover_is_bounded_by_the_period(tmp_path: Path) -> None:
	"""Rows outside the period are not counted; a row without block time uses first seen."""

	async def scenario() -> tuple[int, int]:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _record(engine, asset_id=assets["usdt"], amount=1_000_000, txid="inside")
			await _record(
				engine, asset_id=assets["usdt"], amount=4_000_000, txid="before",
				tx_time=datetime(2026, 8, 31, 23, 59),
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=5_000_000, txid="after",
				tx_time=datetime(2026, 10, 1),
			)
			# Провайдер не сообщил время блока: строка датируется наблюдением,
			# а не выпадает из всех периодов.
			await _record(
				engine, asset_id=assets["usdt"], amount=6_000_000, txid="undated",
				tx_time=None,
			)
			# Та же безвременная строка, но увиденная в прошлом периоде,
			# принадлежит ему, а не текущему.
			await _record(
				engine, asset_id=assets["usdt"], amount=7_000_000, txid="undated-august",
				tx_time=None, first_seen_at=datetime(2026, 8, 20),
			)
			inside = await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
			august = await billing.user_turnover(
				engine, registry, since=datetime(2026, 8, 1), until=PERIOD_START
			)
			return inside, august
		finally:
			await engine.dispose()
			await registry.dispose()

	inside, august = asyncio.run(scenario())
	assert inside == 1_000_000 + 6_000_000
	assert august == 4_000_000 + 7_000_000


def test_turnover_without_a_configured_asset_list_is_zero(tmp_path: Path) -> None:
	"""Nothing is counted until the operator names the assets; broken settings degrade."""

	async def scenario() -> tuple[int, int]:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _record(engine, asset_id=assets["usdt"], amount=1_000_000, txid="paid")
			await registry_ops.set_setting(
				registry, f"{billing.SETTING_ASSETS_PREFIX}{NETWORK}", ""
			)
			unset = await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
			await registry_ops.set_setting(
				registry, f"{billing.SETTING_ASSETS_PREFIX}{NETWORK}", "{not json"
			)
			broken = await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
			return unset, broken
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == (0, 0)
