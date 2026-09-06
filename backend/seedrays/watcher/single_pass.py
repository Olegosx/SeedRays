"""One watcher pass: range scanning per network with local match filtering.

The pass is two-phase (ADR-0021): the authoritative scan reads only the
finalized zone of the chain and is the sole source of balance changes; the
provisional preview reads the zone above the finality boundary so pending
payments surface immediately, and its rows are removed if the finalized
chain never confirms them (reorganization).

See ADR-0007 (address-centric pass), ADR-0017 (transaction model) and
ADR-0018 (range scanning). Settings live in the registry (ADR-0016).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.chains import tron
from seedrays.chains.base import ChainDataSource, ChainDataSourceError, RangeTransfer, RateLimitedError
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path

logger = logging.getLogger(__name__)

# Ключи настроек (реестр, ADR-0016).
SETTING_API_KEY = "provider.trongrid.api_key"
SETTING_RATE = "provider.trongrid.rate_per_sec"
SETTING_OVERLAP = "watcher.overlap_minutes"
SETTING_SCAN_START = "watcher.scan_start"

DEFAULT_RATE_PER_SEC = 3.0
DEFAULT_OVERLAP_MINUTES = 10
# Предохранитель на догон нативного сканирования за один проход (~1 час цепочки TRON).
MAX_BLOCKS_PER_PASS = 1200

SourceFactory = Callable[[str, str | None, float], ChainDataSource]


def _default_source_factory(network: str, api_key: str | None, interval: float) -> ChainDataSource:
	"""Create the real data source for a network (TRON family for now)."""
	return tron.create_source(network, api_key=api_key, request_interval=interval)


def _naive_utc(moment: datetime | None) -> datetime | None:
	"""Convert an aware datetime to naive UTC for database storage."""
	if moment is None:
		return None
	return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _aware_utc(moment: datetime) -> datetime:
	"""Interpret a naive database datetime as UTC."""
	return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


@dataclass
class PassStats:
	"""Outcome of one watcher pass."""

	networks_scanned: int = 0
	transfers_matched: int = 0
	rows_recorded: int = 0
	rows_applied: int = 0
	rows_deleted: int = 0
	networks_rate_limited: list[str] = field(default_factory=list)


async def run_pass(
	data_dir: Path, source_factory: SourceFactory = _default_source_factory
) -> PassStats:
	"""Run one watcher pass over every network that has bindings.

	Args:
		data_dir: The gateway data directory.
		source_factory: Data source constructor; tests substitute a fake one.

	Returns:
		Statistics of the pass.
	"""
	stats = PassStats()
	pass_started = datetime.now(timezone.utc)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	user_engines: list[AsyncEngine] = []
	try:
		api_key = await registry_ops.get_setting(registry, SETTING_API_KEY)
		if api_key is None:
			# ВРЕМЕННЫЙ обход до панели оператора: ключ из окружения (ADR-0016).
			api_key = os.environ.get("TRONGRID_API_KEY")
		rate = float(await registry_ops.get_setting(registry, SETTING_RATE) or DEFAULT_RATE_PER_SEC)
		interval = 1.0 / rate if rate > 0 else 0.0
		overlap = timedelta(
			minutes=float(
				await registry_ops.get_setting(registry, SETTING_OVERLAP)
				or DEFAULT_OVERLAP_MINUTES
			)
		)
		scan_start_raw = await registry_ops.get_setting(registry, SETTING_SCAN_START)
		scan_start = (
			_aware_utc(datetime.fromisoformat(scan_start_raw)) if scan_start_raw else pass_started
		)

		# Локальный фильтр сопоставления: сеть → адрес → база владельца (ADR-0018).
		match_index: dict[str, dict[str, AsyncEngine]] = {}
		for user in await registry_ops.list_users(registry):
			db_path = user_db_path(data_dir, user.directory)
			if not db_path.exists():
				logger.warning("user %s has no database at %s, skipping", user.login, db_path)
				continue
			engine = create_sqlite_engine(db_path)
			user_engines.append(engine)
			for binding in await user_store.list_binding_addresses(engine):
				match_index.setdefault(binding.network, {})[binding.address] = engine

		for network, addresses in sorted(match_index.items()):
			if network not in tron.NETWORK_BASE_URLS:
				logger.warning("network %s: no data source implementation, skipping", network)
				continue
			try:
				recorded, matched, applied, deleted = await _scan_network(
					registry=registry,
					network=network,
					addresses=addresses,
					source=source_factory(network, api_key, interval),
					since_default=scan_start,
					overlap=overlap,
					pass_started=pass_started,
				)
			except RateLimitedError as exc:
				logger.warning("network %s: rate limited, postponing: %s", network, exc)
				stats.networks_rate_limited.append(network)
				continue
			except ChainDataSourceError as exc:
				logger.error("network %s: scan failed: %s", network, exc)
				continue
			stats.networks_scanned += 1
			stats.rows_recorded += recorded
			stats.transfers_matched += matched
			stats.rows_applied += applied
			stats.rows_deleted += deleted
	finally:
		for engine in user_engines:
			await engine.dispose()
		await registry.dispose()
	return stats


async def _scan_network(
	*,
	registry: AsyncEngine,
	network: str,
	addresses: dict[str, AsyncEngine],
	source: ChainDataSource,
	since_default: datetime,
	overlap: timedelta,
	pass_started: datetime,
) -> tuple[int, int, int, int]:
	"""Scan one network in two phases (ADR-0021) and store the matches.

	Phase 1 — the authoritative scan of the finalized zone: records rows as
	finalized, removes provisional rows the finalized chain did not confirm
	(reorganization) and applies balances. Phase 2 — the provisional
	preview of the zone above the finality boundary: best-effort, its
	failure never blocks the authoritative progress.

	Returns:
		(rows recorded, transfers matched, rows applied, rows deleted).
	"""
	try:
		boundary = await source.finality_boundary()
		head = await source.latest_block()
		state = await registry_ops.get_watcher_state(registry, network)
		contracts = await _watched_contracts(registry, network)

		# Фаза 1: авторитетное сканирование финализированной зоны.
		final_since = (
			_aware_utc(state.last_scan_at) if state and state.last_scan_at else since_default
		) - overlap
		confirmed: list[RangeTransfer] = []
		for contract in contracts:
			confirmed.extend(
				await source.token_transfers(
					contract["contract"],
					contract["symbol"],
					int(contract["decimals"]),
					final_since,
					confirmed=True,
				)
			)
		final_start = state.last_block + 1 if state else boundary.block_number + 1
		final_end = min(boundary.block_number, final_start + MAX_BLOCKS_PER_PASS)
		if boundary.block_number - final_start > MAX_BLOCKS_PER_PASS:
			logger.warning(
				"network %s: %d finalized blocks behind, catching up %d per pass",
				network,
				boundary.block_number - final_start,
				MAX_BLOCKS_PER_PASS,
			)
		if final_start <= final_end:
			confirmed.extend(await source.native_transfers(final_start, final_end))
		matched, recorded = await _record_transfers(
			confirmed, addresses, registry, finalized_at=_naive_utc(pass_started)
		)

		# Чистка: предварительные строки внутри уже просканированной
		# финализированной зоны, которые она не подтвердила, — жертвы
		# перестройки цепи (политика ADR-0021).
		scanned_final = final_end if final_start <= final_end else (
			state.last_block if state else boundary.block_number
		)
		asset_ids = {a.id for a in await registry_ops.list_assets(registry, network)}
		deleted = 0
		for engine in set(addresses.values()):
			for txid in await user_store.delete_unfinalized(
				engine, asset_ids=asset_ids, up_to_block=scanned_final
			):
				logger.warning(
					"network %s: provisional tx %s not confirmed by the finalized "
					"chain (reorg), removed",
					network,
					txid,
				)
				deleted += 1

		applied = 0
		for engine in set(addresses.values()):
			applied += await user_store.apply_finalized(
				engine, asset_ids=asset_ids, applied_at=_naive_utc(pass_started)
			)

		await registry_ops.set_watcher_state(
			registry,
			network,
			last_block=scanned_final,
			last_scan_at=_naive_utc(pass_started),
		)

		# Фаза 2: предпросмотр зоны выше границы финальности — чтобы платёж
		# был виден как ожидающий сразу. Сбой предпросмотра не срывает проход:
		# курсоры уже записаны, зона будет пересканирована следующим проходом.
		try:
			preview: list[RangeTransfer] = []
			for contract in contracts:
				preview.extend(
					await source.token_transfers(
						contract["contract"],
						contract["symbol"],
						int(contract["decimals"]),
						None,
						confirmed=False,
					)
				)
			if boundary.block_number < head:
				preview.extend(
					await source.native_transfers(
						boundary.block_number + 1,
						min(head, boundary.block_number + MAX_BLOCKS_PER_PASS),
					)
				)
			preview_matched, preview_recorded = await _record_transfers(
				preview, addresses, registry, finalized_at=None
			)
			matched += preview_matched
			recorded += preview_recorded
		except RateLimitedError as exc:
			logger.warning("network %s: preview scan rate limited, skipped: %s", network, exc)
		except ChainDataSourceError as exc:
			logger.error("network %s: preview scan failed, skipped: %s", network, exc)

		logger.info(
			"network %s: head=%d boundary=%d matched=%d recorded=%d applied=%d deleted=%d",
			network,
			head,
			boundary.block_number,
			matched,
			recorded,
			applied,
			deleted,
		)
		return recorded, matched, applied, deleted
	finally:
		await source.aclose()


async def _record_transfers(
	transfers: list[RangeTransfer],
	addresses: dict[str, AsyncEngine],
	registry: AsyncEngine,
	*,
	finalized_at: datetime | None,
) -> tuple[int, int]:
	"""Match transfers against tracked addresses and store the hits.

	Both legs of a transfer are checked: an address may be the recipient,
	the sender, or both (a self-transfer records two rows).

	Returns:
		(transfers matched, rows recorded).
	"""
	matched = recorded = 0
	for transfer in transfers:
		for address, direction in (
			(transfer.to_address, "in"),
			(transfer.from_address, "out"),
		):
			engine = addresses.get(address)
			if engine is None:
				continue
			matched += 1
			asset = await registry_ops.get_or_create_asset(
				registry,
				network=transfer.asset.network,
				kind="native" if transfer.asset.contract_address == "" else "token",
				contract_address=transfer.asset.contract_address,
				symbol=transfer.asset.symbol,
				decimals=transfer.asset.decimals,
			)
			inserted = await user_store.record_transaction(
				engine,
				address=address,
				txid=transfer.txid,
				asset_id=asset.id,
				direction=direction,
				amount=transfer.amount,
				block_number=transfer.block_number,
				tx_time=_naive_utc(transfer.timestamp),
				status=transfer.status.value,
				event_index=transfer.event_index,
				finalized_at=finalized_at,
			)
			recorded += int(inserted)
	return matched, recorded


async def _watched_contracts(registry: AsyncEngine, network: str) -> list[dict]:
	"""Token contracts to scan: the operator setting plus known catalog assets.

	The setting ``watcher.contracts.<network>`` holds a JSON list of
	``{"contract", "symbol", "decimals"}`` objects (events carry no token
	metadata, so the metadata comes from here or from the catalog).
	"""
	contracts: dict[str, dict] = {}
	raw = await registry_ops.get_setting(registry, f"watcher.contracts.{network}")
	if raw:
		try:
			for entry in json.loads(raw):
				contracts[entry["contract"]] = entry
		except (ValueError, TypeError, KeyError) as exc:
			logger.error("invalid watcher.contracts.%s setting ignored: %s", network, exc)
	for asset in await registry_ops.list_assets(registry, network):
		if asset.kind == "token":
			contracts.setdefault(
				asset.contract_address,
				{
					"contract": asset.contract_address,
					"symbol": asset.symbol,
					"decimals": asset.decimals,
				},
			)
	return list(contracts.values())
