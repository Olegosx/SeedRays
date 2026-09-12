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


async def _bind(engine: AsyncEngine, *addresses: str) -> None:
	"""Make the given addresses bound addresses of this owner.

	Нужен там, где проверяется оборот: перекладывание между своими
	адресами узнаётся по тому, что контрагент строки сам привязан к
	этому владельцу.
	"""
	async with engine.begin() as conn:
		await conn.execute(insert(schema_user.wallets).values(family="tron", xpub="xpub"))
		await conn.execute(
			insert(schema_user.applications).values(name="shop", key_hash="k1")
		)
		# По пользователю приложения на адрес: в одной сети у одного
		# владельца приложения адрес ровно один (ограничение схемы).
		for index, address in enumerate(addresses, start=1):
			await conn.execute(
				insert(schema_user.app_users).values(
					application_id=1, external_id=f"u{index}"
				)
			)
			await conn.execute(
				insert(schema_user.bindings).values(
					wallet_id=1,
					network=NETWORK,
					address=address,
					application_id=1,
					app_user_id=index,
					derivation_index=index - 1,
				)
			)


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
	counterparty: str | None = None,
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
				counterparty=counterparty,
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
	"""Money arriving from one's own bound address is a move, not income."""

	async def scenario() -> int:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _bind(engine, "TAddrFrom", "TAddrTo")
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="move",
				direction="out", address="TAddrFrom", counterparty="TAddrTo",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="move",
				address="TAddrTo", counterparty="TAddrFrom",
			)
			# Платёж постороннего ровно той же суммы: прежнее правило,
			# смотревшее на совпадение суммы, выбрасывало из оборота и его.
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="batch",
				address="TAddrTo", counterparty="TPayer",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=3_000_000, txid="payment",
				address="TAddrTo", counterparty="TPayer",
			)
			return await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == 5_000_000


def test_a_paired_own_leg_no_longer_erases_the_income_beside_it(tmp_path: Path) -> None:
	"""One transaction carrying both a payment and an own move keeps the payment.

	Так выглядела бы попытка не платить комиссию: контракт кладёт в одну
	транзакцию перевод плательщика и равный ему перевод между своими
	адресами. Пока внутренний перевод узнавался по совпадению суммы,
	выпадали оба — и оборот обнулялся целиком.
	"""

	async def scenario() -> int:
		registry, engine, assets = await _seed(tmp_path)
		try:
			await _bind(engine, "TOurA", "TOurB")
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="trick",
				address="TOurA", counterparty="TPayer",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="trick",
				address="TOurB", counterparty="TOurA",
			)
			await _record(
				engine, asset_id=assets["usdt"], amount=2_000_000, txid="trick",
				direction="out", address="TOurA", counterparty="TOurB",
			)
			return await billing.user_turnover(
				engine, registry, since=PERIOD_START, until=PERIOD_END
			)
		finally:
			await engine.dispose()
			await registry.dispose()

	assert asyncio.run(scenario()) == 2_000_000


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


PAY_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class FakePaymentSource:
	"""A chain source that answers the billing check with canned transfers."""

	def __init__(self, transfers_by_address: dict[str, list], *, failing: bool = False) -> None:
		self.network = PAY_NETWORK
		self._transfers = transfers_by_address
		# Недоступный провайдер: опрос отказывает, база при этом цела.
		self._failing = failing
		self.calls: list[tuple[str, object, object]] = []
		self.closed = False

	async def transfers(self, address, since=None, only_confirmed=None):
		self.calls.append((address, since, only_confirmed))
		if self._failing:
			from seedrays.chains.base import ChainDataSourceError

			raise ChainDataSourceError("fake: the provider is unreachable")
		return list(self._transfers.get(address, []))

	async def aclose(self) -> None:
		self.closed = True


def _incoming(address: str, amount: int, *, txid: str, contract: str = PAY_USDT,
              symbol: str = "USDT", decimals: int = 6):
	"""One confirmed incoming transfer as the chain source reports it."""
	from datetime import timezone

	from seedrays.chains.base import AssetInfo, Direction, TransferEvent, TransferStatus

	return TransferEvent(
		network=PAY_NETWORK,
		address=address,
		txid=txid,
		direction=Direction.IN,
		asset=AssetInfo(
			network=PAY_NETWORK, contract_address=contract, symbol=symbol, decimals=decimals
		),
		amount=amount,
		block_number=None,
		timestamp=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
		status=TransferStatus.SUCCESS,
	)


async def _gateway_with_invoice(tmp_path: Path) -> tuple[AsyncEngine, str]:
	"""A gateway with one issued invoice; returns the billing engine and its address."""
	from seedrays.storage import billing as billing_store

	billing_engine = await _billing_gateway(tmp_path)
	registry = create_sqlite_engine(registry_db_path(tmp_path))
	await registry_ops.set_setting(
		registry, f"{billing.SETTING_PAYMENT_ASSETS_PREFIX}{PAY_NETWORK}", f'["{PAY_USDT}"]'
	)
	await registry.dispose()
	await billing.run_pass(tmp_path, now=datetime(2026, 10, 5))
	invoices = await billing_store.list_invoices(billing_engine)
	return billing_engine, invoices[0].address


def test_full_payment_settles_the_invoice(tmp_path: Path) -> None:
	"""The exact amount closes the invoice, and a re-check changes nothing."""

	async def scenario() -> tuple[billing.CheckStats, object, billing.CheckStats]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			# Счёт на 15 USDT: ровно столько и присылают.
			source = FakePaymentSource({address: [_incoming(address, 15 * 10**6, txid="pay1")]})
			first = await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			settled = (await billing_store.list_invoices(billing_engine))[0]
			# Повторный проход видит тот же перевод: ключ идемпотентности не
			# даёт зачесть его дважды.
			again = await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 8), source_factory=lambda n, k, i: source
			)
			return first, settled, again
		finally:
			await billing_engine.dispose()

	first, settled, again = asyncio.run(scenario())
	assert first.payments_recorded == 1
	assert first.invoices_settled == 1
	assert settled.state == "paid"
	assert settled.credited == str(15 * 10**6)
	assert settled.paid_at is not None
	assert again.payments_recorded == 0, "повтор не создаёт второй платёж"
	assert again.addresses_checked == 0, "оплаченный счёт больше не опрашивается"


def test_underpayment_leaves_the_invoice_open(tmp_path: Path) -> None:
	"""Short money is credited but does not settle the invoice; the rest can follow."""

	async def scenario() -> tuple[object, object]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			short = FakePaymentSource({address: [_incoming(address, 10 * 10**6, txid="part1")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: short
			)
			after_part = (await billing_store.list_invoices(billing_engine))[0]
			rest = FakePaymentSource({
				address: [
					_incoming(address, 10 * 10**6, txid="part1"),
					_incoming(address, 5 * 10**6, txid="part2"),
				]
			})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 8), source_factory=lambda n, k, i: rest
			)
			after_rest = (await billing_store.list_invoices(billing_engine))[0]
			return after_part, after_rest
		finally:
			await billing_engine.dispose()

	after_part, after_rest = asyncio.run(scenario())
	assert after_part.state == "issued", "недоплата не открывает доступ"
	assert after_part.credited == str(10 * 10**6)
	assert after_rest.state == "paid", "доплата закрывает счёт"
	assert after_rest.credited == str(15 * 10**6)


def test_overpayment_becomes_credit_for_the_next_invoice(tmp_path: Path) -> None:
	"""What exceeds the invoice stays uncredited and settles the next one."""

	async def scenario() -> tuple[object, object]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			# Присылают 40 USDT на счёт в 15: 25 остаются авансом.
			source = FakePaymentSource({address: [_incoming(address, 40 * 10**6, txid="big")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			first = (await billing_store.list_invoices(billing_engine))[0]
			# Ноябрьский проход выставляет счёт за октябрь; оборот тот же.
			await _record_october_turnover(tmp_path)
			await billing.run_pass(tmp_path, now=datetime(2026, 11, 2))
			await billing.check_payments(
				tmp_path, now=datetime(2026, 11, 3), source_factory=lambda n, k, i: source
			)
			rows = await billing_store.list_invoices(billing_engine)
			return first, rows[0]
		finally:
			await billing_engine.dispose()

	first, second = asyncio.run(scenario())
	assert first.state == "paid"
	assert second.period_start == datetime(2026, 10, 1)
	assert second.state == "paid", "аванс закрыл следующий счёт без нового перевода"


async def _record_october_turnover(tmp_path: Path) -> None:
	"""Give the user an October turnover equal to the September one."""
	registry = create_sqlite_engine(registry_db_path(tmp_path))
	user = await registry_ops.get_user_by_login(registry, "alice")
	assets = await registry_ops.list_assets(registry, NETWORK)
	usdt = next(a for a in assets if a.contract_address == USDT)
	engine = create_sqlite_engine(user_db_path(tmp_path, user.directory))
	await _record(
		engine, asset_id=usdt.id, amount=1000 * billing.MICRO_USDT, txid="october",
		tx_time=datetime(2026, 10, 15), first_seen_at=datetime(2026, 10, 15),
	)
	await engine.dispose()
	await registry.dispose()


def test_foreign_asset_is_recorded_but_not_credited(tmp_path: Path) -> None:
	"""A stray token on an invoice address never settles anything."""

	async def scenario() -> tuple[billing.CheckStats, object]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			source = FakePaymentSource({
				address: [
					_incoming(
						address, 99 * 10**6, txid="spam",
						contract="TSpamContract00000000000000000000", symbol="USDT",
					)
				]
			})
			stats = await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			invoice = (await billing_store.list_invoices(billing_engine))[0]
			return stats, invoice
		finally:
			await billing_engine.dispose()

	stats, invoice = asyncio.run(scenario())
	assert stats.foreign_assets == 1
	assert stats.invoices_settled == 0
	assert invoice.state == "issued"
	assert invoice.credited == "0"


def test_check_polls_only_addresses_with_unpaid_invoices(tmp_path: Path) -> None:
	"""No unpaid invoice — no provider request at all."""

	async def scenario() -> tuple[billing.CheckStats, list]:
		upgrade_all(tmp_path)
		source = FakePaymentSource({})
		stats = await billing.check_payments(
			tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
		)
		return stats, source.calls

	stats, calls = asyncio.run(scenario())
	assert stats.addresses_checked == 0
	assert calls == [], "без неоплаченных счетов провайдера не беспокоим"


def test_check_asks_for_confirmed_transfers_from_the_cursor(tmp_path: Path) -> None:
	"""The check asks for confirmed transfers only, resuming from the address cursor."""

	async def scenario() -> list:
		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			source = FakePaymentSource({address: []})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 8), source_factory=lambda n, k, i: source
			)
			return source.calls
		finally:
			await billing_engine.dispose()

	calls = asyncio.run(scenario())
	assert [c[2] for c in calls] == [True, True], "спрашиваем только подтверждённые"
	assert calls[0][1] is None, "первый опрос идёт без нижней границы"
	# Второй опрос продолжает от прошлой отметки с перекрытием.
	assert calls[1][1] == datetime(2026, 10, 7) - billing.CHECK_OVERLAP


async def _client_for_alice(tmp_path: Path):
	"""A signed-in cabinet client of the user who owes an invoice."""
	import httpx

	from seedrays.api.app_api import create_app
	from tests.seeding import TEST_CAPTCHA_COST, captcha_solution, enable_dev_mail

	await enable_dev_mail(tmp_path)
	transport = httpx.ASGITransport(
		app=create_app(tmp_path, captcha_cost=TEST_CAPTCHA_COST)
	)
	client = httpx.AsyncClient(transport=transport, base_url="https://gw")
	registry = create_sqlite_engine(registry_db_path(tmp_path))
	# Пользователь уже создан посевом биллинга; даём ему пароль и почту для входа.
	from seedrays.orchestrator import auth

	user = await registry_ops.get_user_by_login(registry, "alice")
	await registry_ops.set_user_password(registry, user.id, auth._hasher.hash("correct-horse"))
	await registry_ops.add_user_email(
		registry, user_id=user.id, address="a@example.com", is_primary=True,
		confirm_token_hash=None, confirm_expires_at=None,
		confirmed_at=datetime(2026, 9, 1),
	)
	await registry.dispose()
	login = await client.post(
		"/v1/user/login",
		json={
			"identifier": "alice",
			"password": "correct-horse",
			"captcha": await captcha_solution(client),
		},
	)
	assert login.status_code == 200, login.text
	return client, login.json()["csrf"]


def test_overdue_invoice_closes_the_cabinet_except_payment(tmp_path: Path) -> None:
	"""A suspended user keeps the account and sign-out routes, loses the rest."""

	async def scenario() -> tuple[int, int, int, int]:
		billing_engine, _address = await _gateway_with_invoice(tmp_path)
		try:
			client, csrf = await _client_for_alice(tmp_path)
			before = await client.get("/v1/user/wallets")
			# Срок оплаты — 12 октября; проход 20-го застаёт счёт просроченным.
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			wallets = await client.get("/v1/user/wallets")
			me = await client.get("/v1/user/me")
			logout = await client.post("/v1/user/logout", headers={"X-CSRF-Token": csrf})
			await client.aclose()
			return before.status_code, wallets.status_code, me.status_code, logout.status_code
		finally:
			await billing_engine.dispose()

	before, wallets, me, logout = asyncio.run(scenario())
	assert before == 200, "до просрочки кабинет открыт"
	assert wallets == 403, "после просрочки обычные разделы закрыты"
	assert me == 200, "чтение учётной записи остаётся доступным"
	assert logout == 200, "выйти можно всегда"


def test_overdue_invoice_closes_the_application_api(tmp_path: Path) -> None:
	"""The application key of a suspended owner stops working, with a clear code."""

	async def scenario() -> tuple[int, str, int]:
		import httpx
		from sqlalchemy import insert

		from seedrays.api.app_api import create_app
		from seedrays.orchestrator.operations import hash_api_key
		from seedrays.storage import schema_registry, schema_user

		billing_engine, _address = await _gateway_with_invoice(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			key_hash = hash_api_key("app-key-0001")
			async with registry.begin() as conn:
				await conn.execute(
					insert(schema_registry.api_keys).values(key_hash=key_hash, user_id=user.id)
				)
			await registry.dispose()
			engine = create_sqlite_engine(user_db_path(tmp_path, user.directory))
			async with engine.begin() as conn:
				await conn.execute(
					insert(schema_user.applications).values(name="shop", key_hash=key_hash)
				)
			await engine.dispose()

			transport = httpx.ASGITransport(app=create_app(tmp_path))
			client = httpx.AsyncClient(transport=transport, base_url="https://gw")
			headers = {"X-API-Key": "app-key-0001"}
			before = await client.get("/v1/app/users", headers=headers)
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			after = await client.get("/v1/app/users", headers=headers)
			await client.aclose()
			return before.status_code, after.json()["error"]["code"], after.status_code
		finally:
			await billing_engine.dispose()

	before, code, status = asyncio.run(scenario())
	assert before == 200
	assert status == 403
	assert code == "billing_suspended"


def test_payment_restores_access_without_the_operator(tmp_path: Path) -> None:
	"""Settling the invoice reopens the gateway in the same pass.

	Проход — это проверка платежей и следом сверка состояний, ровно как их
	вызывает часовой цикл: доступ приводится в соответствие состоянию
	счетов одной точкой, а не побочным эффектом зачёта.
	"""

	async def scenario() -> tuple[str, str, int]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			await registry.dispose()
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			suspended = await billing_store.access_state(billing_engine, user.id)
			source = FakePaymentSource({address: [_incoming(address, 15 * 10**6, txid="pay")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 21), source_factory=lambda n, k, i: source
			)
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 21))
			restored = await billing_store.access_state(billing_engine, user.id)

			client, _csrf = await _client_for_alice(tmp_path)
			wallets = await client.get("/v1/user/wallets")
			await client.aclose()
			return suspended, restored, wallets.status_code
		finally:
			await billing_engine.dispose()

	suspended, restored, wallets = asyncio.run(scenario())
	assert suspended == "suspended"
	assert restored == "ok"
	assert wallets == 200, "после оплаты кабинет снова открыт"


def test_access_stays_closed_while_another_invoice_is_unpaid(tmp_path: Path) -> None:
	"""Settling one debt does not reopen the gateway while another one stands."""

	async def scenario() -> str:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			await registry.dispose()
			# Второй счёт — за октябрь; оба остаются неоплаченными и просрочены.
			await _record_october_turnover(tmp_path)
			await billing.run_pass(tmp_path, now=datetime(2026, 11, 2))
			await billing.run_pass(tmp_path, now=datetime(2026, 11, 20))
			# Платёж ровно на один счёт.
			source = FakePaymentSource({address: [_incoming(address, 15 * 10**6, txid="one")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 11, 21), source_factory=lambda n, k, i: source
			)
			return await billing_store.access_state(billing_engine, user.id)
		finally:
			await billing_engine.dispose()

	assert asyncio.run(scenario()) == "suspended"


def test_two_transfers_of_one_transaction_settle_the_invoice(tmp_path: Path) -> None:
	"""A payment split across one transaction counts in full, not once.

	Порядкового номера события адресный эндпоинт провайдера не сообщает,
	поэтому второй перевод той же транзакции на тот же адрес отбрасывался
	бы как дубль первого: счёт оставался бы недоплаченным, а доступ —
	закрытым при полностью уплаченных деньгах.
	"""

	async def scenario() -> tuple[billing.CheckStats, object]:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			source = FakePaymentSource(
				{
					address: [
						_incoming(address, 10 * 10**6, txid="split"),
						_incoming(address, 5 * 10**6, txid="split"),
					]
				}
			)
			stats = await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			invoice = (await billing_store.list_invoices(billing_engine))[0]
			return stats, invoice
		finally:
			await billing_engine.dispose()

	stats, invoice = asyncio.run(scenario())
	assert stats.payments_recorded == 1, "переводы одной транзакции — один платёж"
	assert invoice.state == "paid"
	assert invoice.credited == str(15 * 10**6)


def test_a_short_answer_is_topped_up_on_the_next_check(tmp_path: Path) -> None:
	"""An answer that held part of a transaction is completed later, never lost."""

	async def scenario() -> object:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			# Первый опрос увидел только один перевод транзакции.
			short = FakePaymentSource({address: [_incoming(address, 10 * 10**6, txid="split")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: short
			)
			# Второй опрос отдал транзакцию целиком.
			full = FakePaymentSource(
				{
					address: [
						_incoming(address, 10 * 10**6, txid="split"),
						_incoming(address, 5 * 10**6, txid="split"),
					]
				}
			)
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 8), source_factory=lambda n, k, i: full
			)
			return (await billing_store.list_invoices(billing_engine))[0]
		finally:
			await billing_engine.dispose()

	invoice = asyncio.run(scenario())
	assert invoice.state == "paid", "сумма платежа дописана, счёт закрыт"
	assert invoice.credited == str(15 * 10**6)


def test_payment_is_valued_by_the_catalog_not_by_the_answer(tmp_path: Path) -> None:
	"""Decimals come from the asset catalog; a provider cannot revalue money."""

	async def scenario() -> object:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			# Каталог знает USDT с шестью знаками; ответ заявляет ноль —
			# по нему 15 единиц стали бы 15 миллионами USDT.
			source = FakePaymentSource(
				{address: [_incoming(address, 15 * 10**6, txid="pay1", decimals=0)]}
			)
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			return (await billing_store.list_invoices(billing_engine))[0]
		finally:
			await billing_engine.dispose()

	invoice = asyncio.run(scenario())
	assert invoice.credited == str(15 * 10**6), "оценка по каталогу, а не по ответу"


def test_credit_survives_an_unreachable_provider(tmp_path: Path) -> None:
	"""Money already in the database is credited even when the poll fails.

	Иначе переплата прошлого периода не закрыла бы новый счёт, пока
	провайдер недоступен, и пользователь получил бы просрочку при деньгах,
	давно лежащих у владельца шлюза.
	"""

	async def scenario() -> object:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			# Деньги пришли раньше и уже записаны — зачесть их можно, не
			# спрашивая провайдера ни о чём.
			await billing_store.record_payment(
				billing_engine,
				network=PAY_NETWORK,
				address=address,
				txid="paid-earlier",
				asset_id=1,
				amount=15 * 10**6,
				value=15 * 10**6,
				tx_time=datetime(2026, 10, 6),
				finalized_at=datetime(2026, 10, 6),
			)
			unreachable = FakePaymentSource({}, failing=True)
			await billing.check_payments(
				tmp_path,
				now=datetime(2026, 10, 7),
				source_factory=lambda n, k, i: unreachable,
			)
			return (await billing_store.list_invoices(billing_engine))[0]
		finally:
			await billing_engine.dispose()

	invoice = asyncio.run(scenario())
	assert invoice.state == "paid", "зачёт не должен зависеть от доступности провайдера"


def test_access_is_reconciled_with_the_invoices_not_with_one_lucky_step(
	tmp_path: Path,
) -> None:
	"""An overdue invoice closes the gateway on any later pass, not only on the one
	that marked it.

	Прежде доступ закрывался только тем, чьи счета перевёл в просрочку
	именно этот вызов: сбой на шаге приостановки оставлял просроченный счёт
	при открытом доступе навсегда.
	"""

	async def scenario() -> str:
		from seedrays.storage import billing as billing_store

		billing_engine, _address = await _gateway_with_invoice(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			await registry.dispose()
			# Счёт уже просрочен, а доступ открыт — состояние после сбоя.
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			await billing_store.set_access_state(
				billing_engine,
				user_id=user.id,
				state=billing_store.ACCESS_OK,
				suspended_at=None,
			)
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 21))
			return await billing_store.access_state(billing_engine, user.id)
		finally:
			await billing_engine.dispose()

	assert asyncio.run(scenario()) == "suspended"


def test_a_stale_suspension_is_lifted_by_the_reconciliation(tmp_path: Path) -> None:
	"""A user kept out while nothing awaits money is let back in by the next pass."""

	async def scenario() -> str:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			await registry.dispose()
			source = FakePaymentSource({address: [_incoming(address, 15 * 10**6, txid="pay")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			# Счёт оплачен, но доступ остался закрытым — состояние после сбоя.
			await billing_store.set_access_state(
				billing_engine,
				user_id=user.id,
				state=billing_store.ACCESS_SUSPENDED,
				suspended_at=datetime(2026, 10, 6),
			)
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 8))
			return await billing_store.access_state(billing_engine, user.id)
		finally:
			await billing_engine.dispose()

	assert asyncio.run(scenario()) == "ok"


def test_a_rate_finer_than_the_scale_is_applied_as_written_into_the_invoice(
	tmp_path: Path,
) -> None:
	"""The invoice names the rate it was actually computed with.

	Панель такое значение не принимает, но попасть в базу оно может правкой
	руками. Прежде счёт печатал «0.125», а считал по 0.12 — документ
	противоречил сам себе, и на обороте в миллион расхождение составляло
	50 USDT.
	"""

	async def scenario() -> object:
		upgrade_all(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			await registry_ops.set_setting(registry, billing.SETTING_ENABLED, "1")
			await registry_ops.set_setting(registry, billing.SETTING_RATE, "0.125")
			return await billing.read_terms(registry)
		finally:
			await registry.dispose()

	terms = asyncio.run(scenario())
	assert terms.rate_hundredths == 12
	assert terms.rate_percent == "0.12", "в счёте — применённая ставка, не заявленная"


def test_a_rate_rounding_down_to_zero_stops_the_pass_loudly(tmp_path: Path) -> None:
	"""A rate below the scale issues nothing — and says so, instead of staying silent.

	Прежде множитель обнулялся, проход отрабатывал и не выставлял ни одного
	счёта: в журнале оставалось «выставлено 0», неотличимое от «никто не
	превысил порог».
	"""

	async def scenario() -> object:
		upgrade_all(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			await registry_ops.set_setting(registry, billing.SETTING_ENABLED, "1")
			await registry_ops.set_setting(registry, billing.SETTING_RATE, "0.004")
			return await billing.read_terms(registry)
		finally:
			await registry.dispose()

	assert asyncio.run(scenario()) is None


def test_the_full_amount_is_credited_without_leaving_dust(tmp_path: Path) -> None:
	"""Paying the invoice credits all of it: nothing settles for less than its amount."""

	async def scenario() -> object:
		from seedrays.storage import billing as billing_store

		billing_engine, address = await _gateway_with_invoice(tmp_path)
		try:
			source = FakePaymentSource({address: [_incoming(address, 15 * 10**6, txid="pay")]})
			await billing.check_payments(
				tmp_path, now=datetime(2026, 10, 7), source_factory=lambda n, k, i: source
			)
			return (await billing_store.list_invoices(billing_engine))[0]
		finally:
			await billing_engine.dispose()

	invoice = asyncio.run(scenario())
	assert invoice.state == "paid"
	assert invoice.credited == invoice.amount, "зачтена вся сумма счёта, без остатка"
