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
from seedrays.chains.base import (
	ChainDataSource,
	ChainDataSourceError,
	RangeTooLargeError,
	RangeTransfer,
	RateLimitedError,
)
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.registry import KIND_NATIVE, KIND_TOKEN
from seedrays.storage.user_store import DIRECTION_IN, DIRECTION_OUT
from seedrays.storage.engine import (
	create_sqlite_engine,
	naive_utc,
	registry_db_path,
	user_db_path,
)

logger = logging.getLogger(__name__)

# Ключи настроек (реестр, ADR-0016). Доступ к провайдеру — общий ресурс шлюза,
# поэтому его ключи живут в слое цепочек, а не здесь.
SETTING_OVERLAP = "watcher.overlap_minutes"
SETTING_SCAN_START = "watcher.scan_start"

DEFAULT_OVERLAP_MINUTES = 10
# Предохранитель на догон нативного сканирования за один проход (~1 час цепочки TRON).
MAX_BLOCKS_PER_PASS = 1200
# Предохранитель на догон токен-событий: окно авторитетного скана за один проход.
# После простоя шлюза курсор двигается такими шагами — объём одного вызова
# ограничен и предсказуем, а прогресс фиксируется каждым проходом.
MAX_TOKEN_MINUTES_PER_PASS = 60
# Нижняя граница сужения окна догона. Цена окна измеряется не временем, а
# числом событий в сети, поэтому провайдер может отказать и на дозволенном
# окне; тогда оно делится пополам, но не бесконечно — ниже этого предела
# дробить бессмысленно, и отказ честно уходит наверх.
MIN_TOKEN_MINUTES_PER_PASS = 1

SourceFactory = Callable[[str, str | None, float], ChainDataSource]


def _default_source_factory(network: str, api_key: str | None, interval: float) -> ChainDataSource:
	"""Create the real data source for a network."""
	return chains.create_source(network, api_key=api_key, request_interval=interval)


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
		api_key = await registry_ops.get_setting(registry, chains.SETTING_API_KEY)
		rate = await read_float_setting(registry, chains.SETTING_RATE, chains.DEFAULT_RATE_PER_SEC)
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
		confirmed, token_until, token_caught_up = await _confirmed_token_transfers(
			source,
			contracts,
			network=network,
			token_cursor=token_cursor,
			overlap=overlap,
			pass_started=pass_started,
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
		# Докуда нативный скан доказанно полон. Ответ провайдера может
		# оборваться раньше запрошенного конца, и принять его за «в этих
		# блоках переводов не было» значит потерять их навсегда: перекрытия
		# у нативного скана нет, назад курсор не возвращается.
		native_covered = state.last_block if state else boundary.block_number
		if final_start <= final_end:
			native = await source.native_transfers(final_start, final_end)
			confirmed.extend(native.transfers)
			native_covered = native.covered_through
		failed_engines: set[AsyncEngine] = set()
		matched, recorded = await _record_transfers(
			confirmed,
			addresses,
			registry,
			finalized_at=naive_utc(pass_started),
			failed_engines=failed_engines,
		)

		# Чистка: предварительные строки внутри уже просканированной
		# финализированной зоны, которые она не подтвердила, — жертвы
		# перестройки цепи (политика ADR-0021). Пока токен-скан догоняет,
		# токен-строки не трогаем: их подтверждение ещё впереди.
		scanned_final = native_covered
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
					engine, asset_ids=asset_ids, applied_at=naive_utc(pass_started)
				)
			except SQLAlchemyError:
				# Сбой базы одного владельца не срывает проход по остальным
				# (ADR-0007); его строки доработает следующий проход.
				failed_engines.add(engine)
				logger.exception(
					"network %s: user database failed on apply, skipping this owner",
					network,
				)

		if failed_engines:
			# Курсор — граница доказанной полноты. Пока строки хотя бы одного
			# владельца не записаны, зона не обработана: сдвинуть курсор
			# значит потерять их насовсем (перекрытия у нативного скана нет),
			# а предварительную строку того же перевода следующий проход
			# снесёт как жертву перестройки цепи. Дешевле пересканировать.
			logger.error(
				"network %s: %d owner database(s) failed this pass; the cursor stays at "
				"%s and the range will be scanned again",
				network,
				len(failed_engines),
				state.last_block if state else "unset",
			)
		else:
			await registry_ops.set_watcher_state(
				registry,
				network,
				last_block=scanned_final,
				last_scan_at=naive_utc(pass_started if token_caught_up else token_until),
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
						contract["decimals"],
						None,
						confirmed=False,
					)
				)
			if boundary.block_number < head:
				# Полнота предпросмотра ничего не решает: он ничего не
				# фиксирует и зона пересканируется следующим проходом.
				preview.extend(
					(
						await source.native_transfers(
							boundary.block_number + 1,
							min(head, boundary.block_number + MAX_BLOCKS_PER_PASS),
						)
					).transfers
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


async def _confirmed_token_transfers(
	source: ChainDataSource,
	contracts: list[dict],
	*,
	network: str,
	token_cursor: datetime,
	overlap: timedelta,
	pass_started: datetime,
) -> tuple[list[RangeTransfer], datetime, bool]:
	"""The authoritative token scan of one network, narrowing its window on demand.

	Окно догона задано временем, а его настоящая цена — числом событий,
	которое зависит от сети, а не от шлюза. Провайдер вправе отказать и на
	дозволенном окне; тогда окно делится пополам и запрос повторяется.
	Без этого курсор не двигался бы вовсе: каждый следующий проход просил
	бы тот же диапазон и получал тот же отказ, а сеть стояла бы, пока
	оператор не поправит состояние вручную.

	Args:
		source: The network's data source.
		contracts: Checked entries of the watched-contracts setting.
		network: Network name, for the log lines.
		token_cursor: Lower time bound — where the previous pass stopped.
		overlap: Rescan overlap applied below the cursor.
		pass_started: The moment this pass began.

	Returns:
		(transfers, the window's upper bound, whether the scan caught up).

	Raises:
		RangeTooLargeError: When even the narrowest window is refused.
		ChainDataSourceError: On request failure or unusable response.
		RateLimitedError: When the provider asks to slow down.
	"""
	window = timedelta(minutes=MAX_TOKEN_MINUTES_PER_PASS)
	floor = timedelta(minutes=MIN_TOKEN_MINUTES_PER_PASS)
	while True:
		token_until = token_cursor + window
		caught_up = token_until >= pass_started
		if not caught_up:
			logger.warning(
				"network %s: token scan %s behind, catching up %s per pass",
				network,
				pass_started - token_cursor,
				window,
			)
		try:
			transfers: list[RangeTransfer] = []
			for contract in contracts:
				transfers.extend(
					await source.token_transfers(
						contract["contract"],
						contract["symbol"],
						contract["decimals"],
						token_cursor - overlap,
						confirmed=True,
						until=None if caught_up else token_until,
					)
				)
			return transfers, token_until, caught_up
		except RangeTooLargeError as exc:
			if window <= floor:
				raise
			window = max(window / 2, floor)
			logger.warning(
				"network %s: the token window holds more than one call can return (%s); "
				"narrowing it to %s and asking again",
				network,
				exc,
				window,
			)


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
		# Контрагент строки — вторая сторона перевода: по нему потом
		# видно, что деньги просто переложены между своими адресами.
		for address, direction, counterparty in (
			(transfer.to_address, DIRECTION_IN, transfer.from_address),
			(transfer.from_address, DIRECTION_OUT, transfer.to_address),
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
					tx_time=naive_utc(transfer.timestamp),
					status=transfer.status.value,
					event_index=transfer.event_index,
					counterparty=counterparty,
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


def _contract_entry(entry: object) -> dict | None:
	"""One checked entry of the watched-contracts setting, or None if unusable.

	Проверяется здесь всё, чем сканирование пользуется дальше: ниже по
	проходу значения берутся без оглядки, и одна негодная запись обрушила
	бы проход по всем сетям, а не только свой контракт.
	"""
	if not isinstance(entry, dict):
		return None
	contract, symbol, decimals = entry.get("contract"), entry.get("symbol"), entry.get("decimals")
	if not isinstance(contract, str) or not contract.strip():
		return None
	if not isinstance(symbol, str) or not symbol.strip():
		return None
	# Разрядность строкой оператор пишет часто, и это рабочее значение;
	# булево значение int() принял бы молча, поэтому отсекается отдельно.
	if isinstance(decimals, bool):
		return None
	try:
		places = int(decimals)
	except (TypeError, ValueError):
		return None
	if places < 0:
		return None
	return {"contract": contract.strip(), "symbol": symbol.strip(), "decimals": places}


async def _watched_contracts(registry: AsyncEngine, network: str) -> list[dict]:
	"""Token contracts to scan: the operator setting plus known catalog assets.

	The setting ``watcher.contracts.<network>`` holds a JSON list of
	``{"contract", "symbol", "decimals"}`` objects (events carry no token
	metadata, so the metadata comes from here or from the catalog).

	The setting is edited by hand — the panel deliberately does not offer
	it — so nothing checks it on the way in. Every entry is therefore
	checked here one by one: a broken entry is dropped with its own error
	line and the sound ones keep being scanned, because watching money is
	a continuous duty and a typo in one record must not switch it off for
	the rest.
	"""
	contracts: dict[str, dict] = {}
	raw = await registry_ops.get_setting(registry, f"watcher.contracts.{network}")
	if raw:
		entries: list = []
		try:
			parsed = json.loads(raw)
		except ValueError as exc:
			logger.error("invalid watcher.contracts.%s setting ignored: %s", network, exc)
			parsed = None
		if parsed is not None and not isinstance(parsed, list):
			logger.error(
				"watcher.contracts.%s must be a JSON list, setting ignored", network
			)
		elif parsed is not None:
			entries = parsed
		dropped = 0
		for entry in entries:
			checked = _contract_entry(entry)
			if checked is None:
				dropped += 1
				logger.error(
					"watcher.contracts.%s: entry %r lacks a usable contract, symbol "
					"or decimals, dropped",
					network,
					entry,
				)
				continue
			contracts[checked["contract"]] = checked
		if dropped:
			logger.error(
				"watcher.contracts.%s: %d of %d entries dropped; scanning %s from the setting",
				network,
				dropped,
				len(entries),
				sorted(contracts) or "nothing",
			)
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
