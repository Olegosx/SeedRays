"""End-to-end watcher pass tests on a fake data source and real SQLite databases."""

import asyncio
import json
from datetime import datetime, timezone
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
from seedrays.watcher.single_pass import run_pass

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

	async def token_transfers(self, contract, symbol, decimals, since):
		if self._rate_limited:
			raise RateLimitedError("fake 429")
		return [t for t in self._tokens if t.asset.contract_address == contract]

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

		# Нативные в первый проход не сканируются (курсора ещё нет) — 3 совпадения токенов… нет:
		# t-foreign не наш адрес. Совпали: t-final, t-young. Нативные — со второго прохода.
		assert stats.networks_scanned == 1
		assert stats.transfers_matched == 2
		assert stats.rows_recorded == 2
		assert stats.rows_applied == 1  # t-final (блок 95 ≤ границы 100)

		rows = await _user_rows(tmp_path, schema_user.transactions)
		assert {r.txid for r in rows} == {"t-final", "t-young"}
		applied = {r.txid: r.balance_applied_at for r in rows}
		assert applied["t-final"] is not None
		assert applied["t-young"] is None

		balances = await _user_rows(tmp_path, schema_user.balances)
		assert len(balances) == 1
		assert balances[0].balance == "5000000"
		assert balances[0].total_received == "5000000"

		# Второй проход: те же токены (дубли игнорируются) + нативные из диапазона.
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
		assert stats2.rows_applied == 0  # нативные выше границы 100

		# Третий проход: граница выросла — успешный натив учтён, провал никогда.
		source3 = FakeSource(boundary=114, head=116, tokens=[], natives=[])
		stats3 = await run_pass(tmp_path, source_factory=lambda n, k, i: source3)
		assert stats3.rows_applied == 2  # t-young (105) и n-final (112)

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
