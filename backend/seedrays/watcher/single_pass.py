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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains
from seedrays.chains import tron
from seedrays.chains.base import ChainDataSource, ChainDataSourceError, RangeTransfer, RateLimitedError
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.registry import KIND_NATIVE, KIND_TOKEN
from seedrays.storage.user_store import DIRECTION_IN, DIRECTION_OUT
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
# Предохранитель на догон токен-событий: окно авторитетного скана за один проход.
# После простоя шлюза курсор двигается такими шагами — объём одного вызова
# ограничен и предсказуем, а прогресс фиксируется каждым проходом.
MAX_TOKEN_MINUTES_PER_PASS = 60

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


async def read_float_setting(registry: AsyncEngine, key: str, default: float) -> float:
	"""Read a numeric setting; an invalid value degrades to the default with a log.

	The single pattern for settings the watcher must survive: a broken
	value in the registry must never crash the scanning loop (the same
	policy as the watched-contracts setting).

	Unset and blank are the same thing gateway-wide: the operator panel
	stores a cleared field as an empty string, so treating it as "broken"
	would log an error on every pass for a setting that is simply not set.
	"""
	raw = await registry_ops.get_setting(registry, key)
	if not raw:
		return default
	try:
		return float(raw)
	except ValueError:
		logger.error("invalid %s setting %r ignored, using %s", key, raw, default)
		return default


async def read_datetime_setting(
	registry: AsyncEngine, key: str, default: datetime
) -> datetime:
	"""Read an ISO-datetime setting; an invalid value degrades to the default with a log.

	Unset and blank mean the same here as for numeric settings.
	"""
	raw = await registry_ops.get_setting(registry, key)
	if not raw:
		return default
	try:
		return _aware_utc(datetime.fromisoformat(raw))
	except ValueError:
		logger.error("invalid %s setting %r ignored, using %s", key, raw, default)
		return default


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
		# Ключ провайдера — только из настроек реестра (ADR-0016);
		# вносится оператором через панель.
		api_key = await registry_ops.get_setting(registry, SETTING_API_KEY)
		rate = await read_float_setting(registry, SETTING_RATE, DEFAULT_RATE_PER_SEC)
		interval = 1.0 / rate if rate > 0 else 0.0
		overlap = timedelta(
			minutes=await read_float_setting(registry, SETTING_OVERLAP, DEFAULT_OVERLAP_MINUTES)
		)
		scan_start = await read_datetime_setting(registry, SETTING_SCAN_START, pass_started)

		# Локальный фильтр сопоставления: сеть → адрес → база владельца (ADR-0018).
		# Сбой базы одного пользователя не срывает проход по остальным (ADR-0007).
		match_index: dict[str, dict[str, AsyncEngine]] = {}
		for user in await registry_ops.list_users(registry):
			db_path = user_db_path(data_dir, user.directory)
			if not db_path.exists():
				logger.warning("user %s has no database at %s, skipping", user.login, db_path)
				continue
			engine = create_sqlite_engine(db_path)
			user_engines.append(engine)
			try:
				user_bindings = await user_store.list_binding_addresses(engine)
			except SQLAlchemyError:
				logger.exception(
					"user %s: database unreadable, skipping this pass", user.login
				)
				continue
			for binding in user_bindings:
				match_index.setdefault(binding.network, {})[binding.address] = engine

		for network, addresses in sorted(match_index.items()):
			if network not in chains.supported_networks():
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
		token_cursor = (
			_aware_utc(state.last_scan_at) if state and state.last_scan_at else since_default
		)
		# Окно догона: после простоя курсор двигается шагами ограниченной
		# длины — объём одного прохода предсказуем, прогресс фиксируется.
		token_until = token_cursor + timedelta(minutes=MAX_TOKEN_MINUTES_PER_PASS)
		token_caught_up = token_until >= pass_started
		if not token_caught_up:
			logger.warning(
				"network %s: token scan %s behind, catching up %d minutes per pass",
				network,
				pass_started - token_cursor,
				MAX_TOKEN_MINUTES_PER_PASS,
			)
		confirmed: list[RangeTransfer] = []
		for contract in contracts:
			confirmed.extend(
				await source.token_transfers(
					contract["contract"],
					contract["symbol"],
					int(contract["decimals"]),
					token_cursor - overlap,
					confirmed=True,
					until=None if token_caught_up else token_until,
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
		failed_engines: set[AsyncEngine] = set()
		matched, recorded = await _record_transfers(
			confirmed,
			addresses,
			registry,
			finalized_at=_naive_utc(pass_started),
			failed_engines=failed_engines,
		)

		# Чистка: предварительные строки внутри уже просканированной
		# финализированной зоны, которые она не подтвердила, — жертвы
		# перестройки цепи (политика ADR-0021). Пока токен-скан догоняет,
		# токен-строки не трогаем: их подтверждение ещё впереди.
		scanned_final = final_end if final_start <= final_end else (
			state.last_block if state else boundary.block_number
		)
		catalog = await registry_ops.list_assets(registry, network)
		asset_ids = {a.id for a in catalog}
		cleanup_ids = {
			a.id for a in catalog if a.kind == KIND_NATIVE or token_caught_up
		}
		deleted = 0
		applied = 0
		for engine in set(addresses.values()) - failed_engines:
			try:
				for txid in await user_store.delete_unfinalized(
					engine, asset_ids=cleanup_ids, up_to_block=scanned_final
				):
					logger.warning(
						"network %s: provisional tx %s not confirmed by the finalized "
						"chain (reorg), removed",
						network,
						txid,
					)
					deleted += 1
				applied += await user_store.apply_finalized(
					engine, asset_ids=asset_ids, applied_at=_naive_utc(pass_started)
				)
			except SQLAlchemyError:
				# Сбой базы одного владельца не срывает проход по остальным
				# (ADR-0007); его строки доработает следующий проход.
				failed_engines.add(engine)
				logger.exception(
					"network %s: user database failed on apply, skipping this owner",
					network,
				)

		await registry_ops.set_watcher_state(
			registry,
			network,
			last_block=scanned_final,
			last_scan_at=_naive_utc(pass_started if token_caught_up else token_until),
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
				preview,
				addresses,
				registry,
				finalized_at=None,
				failed_engines=failed_engines,
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
	failed_engines: set[AsyncEngine],
) -> tuple[int, int]:
	"""Match transfers against tracked addresses and store the hits.

	Both legs of a transfer are checked: an address may be the recipient,
	the sender, or both (a self-transfer records two rows). A failing user
	database excludes that owner for the rest of the pass
	(``failed_engines``) without stopping the others (ADR-0007).

	Returns:
		(transfers matched, rows recorded).
	"""
	matched = recorded = 0
	for transfer in transfers:
		for address, direction in (
			(transfer.to_address, DIRECTION_IN),
			(transfer.from_address, DIRECTION_OUT),
		):
			engine = addresses.get(address)
			if engine is None or engine in failed_engines:
				continue
			matched += 1
			asset = await registry_ops.get_or_create_asset(
				registry,
				network=transfer.asset.network,
				kind=KIND_NATIVE if transfer.asset.contract_address == "" else KIND_TOKEN,
				contract_address=transfer.asset.contract_address,
				symbol=transfer.asset.symbol,
				decimals=transfer.asset.decimals,
			)
			try:
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
			except SQLAlchemyError:
				failed_engines.add(engine)
				logger.exception(
					"user database write failed for tx %s, skipping this owner "
					"for the rest of the pass",
					transfer.txid,
				)
				continue
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
		if asset.kind == KIND_TOKEN:
			contracts.setdefault(
				asset.contract_address,
				{
					"contract": asset.contract_address,
					"symbol": asset.symbol,
					"decimals": asset.decimals,
				},
			)
	return list(contracts.values())
