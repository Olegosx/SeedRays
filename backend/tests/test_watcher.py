"""End-to-end watcher pass tests on a fake data source and real SQLite databases."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import insert, select

from seedrays.chains.base import (
	AssetInfo,
	ChainDataSource,
	FinalityBoundary,
	RangeTransfer,
	RateLimitedError,
	TransferStatus,
)
from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_user
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_registry
from seedrays.watcher.single_pass import (
	read_datetime_setting,
	read_float_setting,
	run_pass,
)

NETWORK = "tron-nile"
OUR_ADDRESS = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
OTHER = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_ASSET = AssetInfo(network=NETWORK, contract_address=USDT, symbol="USDT", decimals=6)
TRX_ASSET = AssetInfo(network=NETWORK, contract_address="", symbol="TRX", decimals=6)
TS = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _usdt(txid: str, to: str, amount: int, block: int) -> RangeTransfer:
	return RangeTransfer(
		network=NETWORK,
		txid=txid,
		from_address=OTHER,
		to_address=to,
		asset=USDT_ASSET,
		amount=amount,
		block_number=block,
		timestamp=TS,
		status=TransferStatus.SUCCESS,
	)


def _native(txid: str, to: str, from_: str, amount: int, block: int, ok: bool) -> RangeTransfer:
	return RangeTransfer(
		network=NETWORK,
		txid=txid,
		from_address=from_,
		to_address=to,
		asset=TRX_ASSET,
		amount=amount,
		block_number=block,
		timestamp=TS,
		status=TransferStatus.SUCCESS if ok else TransferStatus.FAILED,
	)


class FakeSource(ChainDataSource):
	"""In-memory data source: preset boundary, head and transfers."""

	def __init__(
		self,
		boundary: int,
		head: int,
		tokens: list[RangeTransfer] = (),
		natives: list[RangeTransfer] = (),
		rate_limited: bool = False,
	) -> None:
		self.network = NETWORK
		self._boundary = boundary
		self._head = head
		self._tokens = list(tokens)
		self._natives = list(natives)
		self._rate_limited = rate_limited

	async def aclose(self) -> None:
		pass

	async def latest_block(self) -> int:
		return self._head

	async def finality_boundary(self) -> FinalityBoundary:
		return FinalityBoundary(block_number=self._boundary, timestamp=TS)

	async def transfers(self, address, since=None, only_confirmed=None):
		return []

	async def token_transfers(self, contract, symbol, decimals, since, *, confirmed, until=None):
		if self._rate_limited:
			raise RateLimitedError("fake 429")
		# Как у провайдера: confirmed — события не выше границы финальности,
		# unconfirmed — только зона выше неё.
		return [
			t
			for t in self._tokens
			if t.asset.contract_address == contract
			and (t.block_number <= self._boundary) == confirmed
		]

	async def native_transfers(self, start_block, end_block):
		return [t for t in self._natives if start_block <= t.block_number <= end_block]


async def _prepare(data_dir: Path) -> None:
	"""Registry + one user with one binding + the watched-contracts setting."""
	upgrade_registry(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	await registry_ops.create_user(registry, data_dir, "alice", "hash")
	await registry_ops.set_setting(
		registry,
		f"watcher.contracts.{NETWORK}",
		json.dumps([{"contract": USDT, "symbol": "USDT", "decimals": 6}]),
	)
	await registry.dispose()

	user = create_sqlite_engine(user_db_path(data_dir, "u1"))
	async with user.begin() as conn:
		await conn.execute(insert(schema_user.wallets).values(family="tron", xpub="xpub"))
		await conn.execute(insert(schema_user.applications).values(name="shop", key_hash="k1"))
		await conn.execute(
			insert(schema_user.app_users).values(application_id=1, external_id="user1")
		)
		await conn.execute(
			insert(schema_user.bindings).values(
				wallet_id=1,
				network=NETWORK,
				address=OUR_ADDRESS,
				application_id=1,
				app_user_id=1,
				derivation_index=0,
			)
		)
	await user.dispose()


async def _user_rows(data_dir: Path, table):
	engine = create_sqlite_engine(user_db_path(data_dir, "u1"))
	async with engine.connect() as conn:
		rows = (await conn.execute(select(table))).all()
	await engine.dispose()
	return rows


def test_pass_records_matches_and_applies_finalized(tmp_path: Path) -> None:
	"""The pass stores matched transfers and applies finalized ones to balances."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		source = FakeSource(
			boundary=100,
			head=110,
			tokens=[
				_usdt("t-final", OUR_ADDRESS, 5_000_000, block=95),
				_usdt("t-young", OUR_ADDRESS, 2_000_000, block=105),
				_usdt("t-foreign", OTHER, 9_000_000, block=96),
			],
			natives=[
				_native("n-final", OUR_ADDRESS, OTHER, 7_000_000, block=90, ok=True),
				_native("n-failed", OUR_ADDRESS, OTHER, 1_000_000, block=91, ok=False),
			],
		)
		stats = await run_pass(tmp_path, source_factory=lambda n, k, i: source)

		# Авторитетное сканирование: t-final (95 ≤ границы 100) финализирован
		# и применён; нативные в первый проход не сканируются (курсора нет).
		# Предпросмотр: t-young (105 > границы) записан предварительной строкой.
		assert stats.networks_scanned == 1
		assert stats.transfers_matched == 2
		assert stats.rows_recorded == 2
		assert stats.rows_applied == 1  # t-final

		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert {r.txid for r in rows} == {"t-final", "t-young"}
		by_txid = {r.txid: r for r in rows}
		assert by_txid["t-final"].finalized_at is not None
		assert by_txid["t-final"].balance_applied_at is not None
		assert by_txid["t-young"].finalized_at is None  # предварительная (pending)
		assert by_txid["t-young"].balance_applied_at is None

		balances = await _user_rows(tmp_path, schema_user.balances)
		assert len(balances) == 1
		assert balances[0].balance == "5000000"
		assert balances[0].total_received == "5000000"

		# Второй проход: токен-дубль отброшен, нативные выше границы попадают
		# в предпросмотр предварительными строками.
		source2 = FakeSource(
			boundary=100,
			head=115,
			tokens=[_usdt("t-final", OUR_ADDRESS, 5_000_000, block=95)],
			natives=[
				_native("n-final", OUR_ADDRESS, OTHER, 7_000_000, block=112, ok=True),
				_native("n-failed", OUR_ADDRESS, OTHER, 1_000_000, block=113, ok=False),
			],
		)
		stats2 = await run_pass(tmp_path, source_factory=lambda n, k, i: source2)
		assert stats2.rows_recorded == 2  # только нативные; токен-дубль отброшен
		assert stats2.rows_applied == 0  # всё записанное — выше границы 100

		# Третий проход: граница выросла, финализированная цепь подтверждает
		# все три строки — успешные учтены, провал никогда.
		source3 = FakeSource(
			boundary=114,
			head=116,
			tokens=[_usdt("t-young", OUR_ADDRESS, 2_000_000, block=105)],
			natives=[
				_native("n-final", OUR_ADDRESS, OTHER, 7_000_000, block=112, ok=True),
				_native("n-failed", OUR_ADDRESS, OTHER, 1_000_000, block=113, ok=False),
			],
		)
		stats3 = await run_pass(tmp_path, source_factory=lambda n, k, i: source3)
		assert stats3.rows_applied == 2  # t-young (105) и n-final (112)
		assert stats3.rows_deleted == 0  # всё подтвердилось — чистить нечего

		balances = await _user_rows(tmp_path, schema_user.balances)
		by_asset = {b.asset_id: b for b in balances}
		assert len(by_asset) == 2
		amounts = sorted(int(b.balance) for b in balances)
		assert amounts == [5_000_000 + 2_000_000, 7_000_000] or amounts == [
			7_000_000,
			7_000_000,
		]

		rows = await _user_rows(tmp_path, schema_user.transactions)
		failed = [r for r in rows if r.txid == "n-failed"]
		assert failed[0].balance_applied_at is None

	asyncio.run(scenario())


def test_pass_survives_rate_limit(tmp_path: Path) -> None:
	"""A rate-limited network is postponed: no crash, no cursor update."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		source = FakeSource(boundary=100, head=110, rate_limited=True)
		stats = await run_pass(tmp_path, source_factory=lambda n, k, i: source)
		assert stats.networks_rate_limited == [NETWORK]
		assert stats.networks_scanned == 0

		registry = create_sqlite_engine(registry_db_path(tmp_path))
		state = await registry_ops.get_watcher_state(registry, NETWORK)
		await registry.dispose()
		assert state is None  # курсор не сдвинут — следующий проход всё пересканирует

	asyncio.run(scenario())


def test_reorged_provisional_rows_are_removed(tmp_path: Path) -> None:
	"""A provisional row the finalized chain never confirms is deleted (ADR-0021)."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		# Проход 1: перевод виден только в зоне выше границы — предварительная строка.
		source = FakeSource(
			boundary=100, head=110,
			tokens=[_usdt("t-orphan", OUR_ADDRESS, 3_000_000, block=105)],
		)
		await run_pass(tmp_path, source_factory=lambda n, k, i: source)
		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert [r.txid for r in rows] == ["t-orphan"]
		assert rows[0].finalized_at is None

		# Проход 2: граница переросла блок 105, но финализированная цепь
		# транзакцию не подтверждает (реорганизация) — строка удаляется.
		source2 = FakeSource(boundary=114, head=116, tokens=[])
		stats2 = await run_pass(tmp_path, source_factory=lambda n, k, i: source2)
		assert stats2.rows_deleted == 1
		assert stats2.rows_applied == 0
		assert await _user_rows(tmp_path, schema_user.transactions) == []
		assert await _user_rows(tmp_path, schema_user.balances) == []

	asyncio.run(scenario())


def test_self_transfer_records_both_legs(tmp_path: Path) -> None:
	"""A transfer to self records both legs: net zero balance, deposit counted."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		# Первый проход лишь заводит курсор на границе финальности.
		empty = FakeSource(boundary=100, head=100)
		await run_pass(tmp_path, source_factory=lambda n, k, i: empty)

		self_tx = _native("n-self", OUR_ADDRESS, OUR_ADDRESS, 4_000_000, block=110, ok=True)
		source = FakeSource(boundary=120, head=121, natives=[self_tx])
		stats = await run_pass(tmp_path, source_factory=lambda n, k, i: source)
		assert stats.rows_recorded == 2  # обе ноги: in и out

		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert {r.direction for r in rows if r.txid == "n-self"} == {"in", "out"}
		balances = await _user_rows(tmp_path, schema_user.balances)
		assert balances[0].balance == "0"  # нетто нулевое
		assert balances[0].total_received == "4000000"  # приход честно учтён

	asyncio.run(scenario())


def test_batch_transfers_in_one_transaction_are_distinct(tmp_path: Path) -> None:
	"""Several transfers of one asset inside one transaction all count (event_index)."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		first = _usdt("t-batch", OUR_ADDRESS, 1_000_000, block=95)
		second = replace(first, amount=2_500_000, event_index=1)
		source = FakeSource(boundary=100, head=101, tokens=[first, second])
		stats = await run_pass(tmp_path, source_factory=lambda n, k, i: source)
		assert stats.rows_recorded == 2

		balances = await _user_rows(tmp_path, schema_user.balances)
		assert balances[0].balance == "3500000"

	asyncio.run(scenario())


def test_asset_autocatalog(tmp_path: Path) -> None:
	"""Assets observed by the pass are created in the registry catalog once."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		source = FakeSource(
			boundary=100, head=110, tokens=[_usdt("t1", OUR_ADDRESS, 1, block=95)]
		)
		await run_pass(tmp_path, source_factory=lambda n, k, i: source)
		await run_pass(tmp_path, source_factory=lambda n, k, i: source)

		registry = create_sqlite_engine(registry_db_path(tmp_path))
		catalog = await registry_ops.list_assets(registry, NETWORK)
		await registry.dispose()
		contracts = sorted(a.contract_address for a in catalog)
		assert contracts == [USDT]

	asyncio.run(scenario())


def test_token_catchup_window_and_cleanup_guard(tmp_path: Path) -> None:
	"""Catch-up moves the token cursor in bounded steps; token cleanup waits for it."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		# Первый проход заводит курсоры.
		await run_pass(tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=100, head=100))

		# Предварительная токен-строка в финализированной зоне (блок 50):
		# при догоняющем токен-скане её чистить рано.
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		asset = await registry_ops.get_or_create_asset(
			registry, network=NETWORK, kind="token", contract_address=USDT,
			symbol="USDT", decimals=6,
		)
		user = create_sqlite_engine(user_db_path(tmp_path, "u1"))
		from seedrays.storage import user_store

		await user_store.record_transaction(
			user,
			address=OUR_ADDRESS,
			txid="t-limbo",
			asset_id=asset.id,
			direction="in",
			amount=1,
			block_number=50,
			tx_time=None,
			status="success",
		)
		await user.dispose()

		# Курсор токенов откатываем на 5 часов назад — имитация простоя.
		state = await registry_ops.get_watcher_state(registry, NETWORK)
		old_cursor = state.last_scan_at - timedelta(hours=5)
		await registry_ops.set_watcher_state(
			registry, NETWORK, last_block=state.last_block, last_scan_at=old_cursor
		)

		await run_pass(tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=120, head=121))
		after = await registry_ops.get_watcher_state(registry, NETWORK)
		# Догоняющий шаг: курсор продвинулся ровно на окно, а не до «сейчас».
		assert after.last_scan_at == old_cursor + timedelta(minutes=60)
		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert [r.txid for r in rows] == ["t-limbo"]  # токен-чистка отложена

		# Догоняем до конца (5 часов = 5 шагов) — после этого чистка срабатывает.
		for _ in range(5):
			await run_pass(
				tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=120, head=121)
			)
		assert await _user_rows(tmp_path, schema_user.transactions) == []
		await registry.dispose()

	asyncio.run(scenario())


def test_invalid_settings_degrade_to_defaults(tmp_path: Path) -> None:
	"""Broken registry settings are logged and defaulted, never crash the pass."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		await registry_ops.set_setting(registry, "watcher.overlap_minutes", "junk")
		await registry_ops.set_setting(registry, "provider.trongrid.rate_per_sec", "fast")
		await registry_ops.set_setting(registry, "watcher.scan_start", "not-a-date")
		await registry.dispose()

		stats = await run_pass(
			tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=100, head=100)
		)
		assert stats.networks_scanned == 1

	asyncio.run(scenario())


def test_blank_settings_are_treated_as_unset(tmp_path: Path) -> None:
	"""A cleared panel field means "use the default", not "broken value"."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		# Панель пишет снятую несекретную настройку пустой строкой.
		await registry_ops.set_setting(registry, "watcher.overlap_minutes", "")
		await registry_ops.set_setting(registry, "provider.trongrid.rate_per_sec", "")
		await registry_ops.set_setting(registry, "watcher.scan_start", "")
		try:
			assert await read_float_setting(registry, "watcher.overlap_minutes", 10) == 10
			moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
			assert await read_datetime_setting(registry, "watcher.scan_start", moment) == moment
		finally:
			await registry.dispose()

		stats = await run_pass(
			tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=100, head=100)
		)
		assert stats.networks_scanned == 1

	asyncio.run(scenario())


def test_one_broken_user_db_does_not_stop_the_pass(tmp_path: Path) -> None:
	"""A corrupt user database is skipped; other users are still scanned (ADR-0007)."""

	async def scenario() -> None:
		await _prepare(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		await registry_ops.create_user(registry, tmp_path, "bob", "hash")
		await registry.dispose()
		# Портим базу второго пользователя: это больше не SQLite-файл.
		user_db_path(tmp_path, "u2").write_bytes(b"garbage, not a database")

		await run_pass(tmp_path, source_factory=lambda n, k, i: FakeSource(boundary=100, head=100))
		deposit = _usdt("t-alive", OUR_ADDRESS, 1_000_000, block=95)
		stats = await run_pass(
			tmp_path,
			source_factory=lambda n, k, i: FakeSource(boundary=100, head=101, tokens=[deposit]),
		)
		assert stats.networks_scanned == 1
		assert stats.rows_recorded == 1  # платёж первого пользователя записан
		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert [r.txid for r in rows] == ["t-alive"]

	asyncio.run(scenario())
