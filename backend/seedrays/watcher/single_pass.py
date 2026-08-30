"""One watcher pass: range scanning per network with local match filtering.

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
				recorded, matched, applied = await _scan_network(
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
) -> tuple[int, int, int]:
	"""Scan one network by ranges and store the matches.

	Returns:
		(rows recorded, transfers matched, rows applied to balances).
	"""
	try:
		boundary = await source.finality_boundary()
		head = await source.latest_block()
		state = await registry_ops.get_watcher_state(registry, network)
		since = (_aware_utc(state.last_scan_at) if state and state.last_scan_at else since_default) - overlap

		transfers: list[RangeTransfer] = []
		for contract in await _watched_contracts(registry, network):
			transfers.extend(
				await source.token_transfers(
					contract["contract"], contract["symbol"], int(contract["decimals"]), since
				)
			)

		native_start = state.last_block + 1 if state else head + 1
		native_end = head
		if native_end - native_start > MAX_BLOCKS_PER_PASS:
			logger.warning(
				"network %s: %d blocks behind, catching up %d per pass",
				network,
				native_end - native_start,
				MAX_BLOCKS_PER_PASS,
			)
			native_end = native_start + MAX_BLOCKS_PER_PASS
		if native_start <= native_end:
			transfers.extend(await source.native_transfers(native_start, native_end))

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
				)
				recorded += int(inserted)

		asset_ids = {a.id for a in await registry_ops.list_assets(registry, network)}
		applied = 0
		for engine in set(addresses.values()):
			applied += await user_store.apply_finalized(
				engine,
				asset_ids=asset_ids,
				boundary_block=boundary.block_number,
				applied_at=_naive_utc(pass_started),
			)

		await registry_ops.set_watcher_state(
			registry,
			network,
			last_block=min(head, native_end) if state or native_start <= native_end else head,
			last_scan_at=_naive_utc(pass_started),
		)
		logger.info(
			"network %s: head=%d boundary=%d matched=%d recorded=%d applied=%d",
			network,
			head,
			boundary.block_number,
			matched,
			recorded,
			applied,
		)
		return recorded, matched, applied
	finally:
		await source.aclose()


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
