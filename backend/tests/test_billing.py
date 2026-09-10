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


# Второй эталонный вектор BIP39 (trezor/python-mnemonic): кошелёк владельца
# должен отличаться от пользовательского, иначе адреса совпали бы.
MASTER_MNEMONIC = (
	"legal winner thank year wave sausage worth useful legal winner thank yellow"
)
PAY_NETWORK = "tron"


def test_fee_applies_to_the_whole_turnover_above_the_threshold() -> None:
	"""The rate hits the entire turnover, and the threshold is a strict boundary."""
	terms = billing.Terms(
		rate_percent="1.5", rate_hundredths=150, threshold=100 * billing.MICRO_USDT,
		due_days=7,
	)
	# 1000 USDT оборота при ставке 1.5% — 15 USDT, а не 13.5 (не «с превышения»).
	assert billing.fee_amount(1000 * billing.MICRO_USDT, terms) == 15 * billing.MICRO_USDT
	# Ровно порог — счёта нет: оборот должен именно превысить его.
	assert billing.fee_amount(100 * billing.MICRO_USDT, terms) == 0
	assert billing.fee_amount(0, terms) == 0


def test_previous_period_is_the_finished_month() -> None:
	"""A pass always bills the period that has already ended."""
	assert billing.previous_period(datetime(2026, 10, 5)) == (
		datetime(2026, 9, 1),
		datetime(2026, 10, 1),
	)
	# Через границу года: январский проход выставляет за декабрь.
	assert billing.previous_period(datetime(2027, 1, 1)) == (
		datetime(2026, 12, 1),
		datetime(2027, 1, 1),
	)


def test_terms_are_off_by_default_and_degrade_on_junk(tmp_path: Path) -> None:
	"""The fee is off until switched on, and a broken rate never crashes a pass."""

	async def scenario() -> tuple[object, object, object]:
		upgrade_all(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			default_off = await billing.read_terms(registry)
			await registry_ops.set_setting(registry, billing.SETTING_ENABLED, "1")
			await registry_ops.set_setting(registry, billing.SETTING_RATE, "1.5")
			await registry_ops.set_setting(registry, billing.SETTING_THRESHOLD, "100")
			await registry_ops.set_setting(registry, billing.SETTING_DUE_DAYS, "not a number")
			terms = await billing.read_terms(registry)
			await registry_ops.set_setting(registry, billing.SETTING_RATE, "0")
			zero_rate = await billing.read_terms(registry)
			return default_off, terms, zero_rate
		finally:
			await registry.dispose()

	default_off, terms, zero_rate = asyncio.run(scenario())
	assert default_off is None, "вознаграждение должно быть выключено по умолчанию"
	assert zero_rate is None, "нулевая ставка — то же самое, что выключено"
	assert terms.rate_hundredths == 150
	assert terms.rate_percent == "1.5"
	assert terms.threshold == 100 * billing.MICRO_USDT
	assert terms.due_days == billing.DEFAULT_DUE_DAYS


async def _master_xpub() -> str:
	from seedrays.families import Family
	from seedrays.keygen.generate import account_xpub

	return account_xpub(MASTER_MNEMONIC, Family.TRON)


def test_master_wallet_and_user_wallet_cannot_share_a_key(tmp_path: Path) -> None:
	"""Both halves of "one xpub — one wallet" hold across the two databases."""

	async def scenario() -> list[str]:
		from seedrays.families import Family
		from seedrays.keygen.generate import account_xpub
		from seedrays.orchestrator import wallets as wallet_ops
		from seedrays.orchestrator.operations import OperationError
		from seedrays.storage.engine import billing_db_path

		upgrade_all(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		billing_engine = create_sqlite_engine(billing_db_path(tmp_path))
		user = await registry_ops.create_user(registry, tmp_path, "alice", "hash")
		engine = create_sqlite_engine(user_db_path(tmp_path, user.directory))
		codes = []
		try:
			master = await _master_xpub()
			await billing.attach_master_wallet(
				billing_engine, registry, network=PAY_NETWORK, xpub=master
			)
			# Пользователь пытается подключить кошелёк шлюза.
			try:
				await wallet_ops.attach_wallet(
					engine, registry, billing_engine,
					user_id=user.id, family="tron", xpub=master, label="",
				)
			except OperationError as exc:
				codes.append(exc.code)
			# И наоборот: владелец берёт ключ, уже подключённый пользователем.
			mine = account_xpub(
				"abandon abandon abandon abandon abandon abandon "
				"abandon abandon abandon abandon abandon about",
				Family.TRON,
			)
			await wallet_ops.attach_wallet(
				engine, registry, billing_engine,
				user_id=user.id, family="tron", xpub=mine, label="",
			)
			try:
				await billing.attach_master_wallet(
					billing_engine, registry, network="tron-nile", xpub=mine
				)
			except OperationError as exc:
				codes.append(exc.code)
			# Тот же ключ владельца во второй сети — тоже отказ.
			try:
				await billing.attach_master_wallet(
					billing_engine, registry, network="tron-nile", xpub=master
				)
			except OperationError as exc:
				codes.append(exc.code)
			return codes
		finally:
			await engine.dispose()
			await billing_engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == ["invalid_xpub", "invalid_xpub", "invalid_xpub"]


def test_invoice_address_is_permanent_and_unique_per_user(tmp_path: Path) -> None:
	"""Each user gets one address per network, and a second call returns the same one."""

	async def scenario() -> tuple[str, str, str, str | None]:
		from seedrays.storage.engine import billing_db_path

		upgrade_all(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		billing_engine = create_sqlite_engine(billing_db_path(tmp_path))
		try:
			missing = await billing.ensure_invoice_address(
				billing_engine, user_id=1, network=PAY_NETWORK
			)
			await billing.attach_master_wallet(
				billing_engine, registry, network=PAY_NETWORK, xpub=await _master_xpub()
			)
			first = await billing.ensure_invoice_address(
				billing_engine, user_id=1, network=PAY_NETWORK
			)
			again = await billing.ensure_invoice_address(
				billing_engine, user_id=1, network=PAY_NETWORK
			)
			other = await billing.ensure_invoice_address(
				billing_engine, user_id=2, network=PAY_NETWORK
			)
			return first, again, other, missing
		finally:
			await billing_engine.dispose()
			await registry.dispose()

	first, again, other, missing = asyncio.run(scenario())
	assert missing is None, "без мастер-кошелька выставлять счёт некуда"
	assert first == again, "адрес счёта постоянный"
	assert first != other, "у разных пользователей разные адреса"


async def _billing_gateway(tmp_path: Path, *, threshold: str = "100") -> AsyncEngine:
	"""A gateway with the fee switched on, a master wallet and one earning user."""
	registry, engine, assets = await _seed(tmp_path)
	from seedrays.storage.engine import billing_db_path

	await registry_ops.set_setting(registry, billing.SETTING_ENABLED, "1")
	await registry_ops.set_setting(registry, billing.SETTING_RATE, "1.5")
	await registry_ops.set_setting(registry, billing.SETTING_THRESHOLD, threshold)
	await registry_ops.set_setting(registry, billing.SETTING_DUE_DAYS, "7")
	billing_engine = create_sqlite_engine(billing_db_path(tmp_path))
	await billing.attach_master_wallet(
		billing_engine, registry, network=PAY_NETWORK, xpub=await _master_xpub()
	)
	# Оборот сентября: 1000 USDT подтверждённых поступлений.
	await _record(engine, asset_id=assets["usdt"], amount=1000 * billing.MICRO_USDT,
	              txid="september")
	await engine.dispose()
	await registry.dispose()
	return billing_engine


def test_pass_issues_one_invoice_per_period(tmp_path: Path) -> None:
	"""The pass bills the finished month once, however often it runs."""

	async def scenario() -> tuple[billing.PassStats, billing.PassStats, list]:
		from seedrays.storage import billing as billing_store

		billing_engine = await _billing_gateway(tmp_path)
		try:
			first = await billing.run_pass(tmp_path, now=datetime(2026, 10, 5))
			second = await billing.run_pass(tmp_path, now=datetime(2026, 10, 6))
			rows = await billing_store.list_invoices(billing_engine)
			return first, second, rows
		finally:
			await billing_engine.dispose()

	first, second, rows = asyncio.run(scenario())
	assert first.invoices_issued == 1
	assert second.invoices_issued == 0, "повторный проход не выставляет счёт заново"
	assert len(rows) == 1
	invoice = rows[0]
	assert invoice.period_start == datetime(2026, 9, 1)
	# 1.5% с оборота 1000 USDT — 15 USDT; ставка и порог заморожены в счёте.
	assert invoice.amount == str(15 * billing.MICRO_USDT)
	assert invoice.turnover == str(1000 * billing.MICRO_USDT)
	assert invoice.rate_percent == "1.5"
	assert invoice.threshold == str(100 * billing.MICRO_USDT)
	assert invoice.due_at == datetime(2026, 10, 12)
	assert invoice.state == "issued"
	assert invoice.address.startswith("T")


def test_pass_skips_a_turnover_below_the_threshold(tmp_path: Path) -> None:
	"""Nothing is billed while the turnover stays under the threshold."""

	async def scenario() -> billing.PassStats:
		billing_engine = await _billing_gateway(tmp_path, threshold="5000")
		try:
			return await billing.run_pass(tmp_path, now=datetime(2026, 10, 5))
		finally:
			await billing_engine.dispose()

	stats = asyncio.run(scenario())
	assert stats.invoices_issued == 0
	assert stats.users_billed == 0


def test_pass_without_a_master_wallet_reports_instead_of_issuing(tmp_path: Path) -> None:
	"""An invoice with nowhere to be paid is not issued, and the owner is told."""

	async def scenario() -> billing.PassStats:
		from seedrays.storage import billing as billing_store
		from seedrays.storage.engine import billing_db_path

		billing_engine = await _billing_gateway(tmp_path)
		try:
			await billing_store.delete_master_wallet(billing_engine, PAY_NETWORK)
			return await billing.run_pass(tmp_path, now=datetime(2026, 10, 5))
		finally:
			await billing_engine.dispose()

	stats = asyncio.run(scenario())
	assert stats.invoices_issued == 0
	assert stats.users_billed == 1
	assert stats.no_master_wallet == ["alice"]


def test_overdue_is_marked_even_when_the_fee_is_switched_off(tmp_path: Path) -> None:
	"""Switching the fee off stops new invoices, not the deadlines of the old ones."""

	async def scenario() -> tuple[billing.PassStats, str]:
		from seedrays.storage import billing as billing_store

		billing_engine = await _billing_gateway(tmp_path)
		try:
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 5))
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			await registry_ops.set_setting(registry, billing.SETTING_ENABLED, "0")
			await registry.dispose()
			# Срок оплаты — 12 октября; проход 20-го застаёт счёт просроченным.
			stats = await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			rows = await billing_store.list_invoices(billing_engine)
			return stats, rows[0].state
		finally:
			await billing_engine.dispose()

	stats, state = asyncio.run(scenario())
	assert stats.invoices_overdue == 1
	assert state == "overdue"
	assert stats.invoices_issued == 0
