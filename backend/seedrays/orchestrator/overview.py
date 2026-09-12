"""Cabinet read operations: incoming history and the dashboard summary.

Amounts leave this module as exact decimal strings computed with integer
arithmetic from the minimal units (never floats) — the same principle as
the Application API (ADR-0011). Database access goes through the storage
layer (ADR-0006); status semantics come from its single point of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.orchestrator.money import format_amount
from seedrays.orchestrator.operations import OperationError
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_views
from seedrays.storage.user_store import API_STATUS_ALL, HISTORY_STATUS_FILTERS, classify_transaction

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 50
# Размер блока «последние операции» на дашборде.
DEFAULT_RECENT_LIMIT = 5


async def _asset_infos(registry: AsyncEngine, asset_ids: set[int]) -> dict[int, dict]:
	"""asset id → the fields the cabinet screens need."""
	records = await registry_ops.get_assets_by_ids(registry, asset_ids)
	# Описание актива берётся из общего реестра, а строки — из базы
	# владельца: разъехаться они могут при восстановлении данных из
	# разновременных копий. Без этой записи строка просто исчезла бы
	# из ответа, молча уменьшив показанную сумму поступлений.
	missing = asset_ids - records.keys()
	if missing:
		logger.warning(
			"assets %s are absent from the registry catalog; their rows are left out",
			sorted(missing),
		)
	return {
		asset_id: {
			"network": record.network,
			"symbol": record.symbol,
			"decimals": record.decimals,
		}
		for asset_id, record in records.items()
	}


@dataclass(frozen=True)
class HistoryRow:
	"""One incoming operation of the cabinet history."""

	time: str | None
	wallet_id: int | None
	wallet: str
	network: str
	asset: str
	amount: str
	txid: str
	status: str


@dataclass(frozen=True)
class HistoryPage:
	"""One page of the history plus the cursor of the next one."""

	rows: list[HistoryRow]
	# Курсор «показать ещё»: (номер блока, ид строки) последней выданной
	# строки; None — страница последняя.
	next_cursor: tuple[int, int] | None


def parse_history_cursor(raw: str) -> tuple[int, int]:
	"""Parse the opaque ``block:id`` cursor of the history pagination.

	Raises:
		OperationError: invalid_cursor.
	"""
	block_part, _, id_part = raw.partition(":")
	try:
		return int(block_part), int(id_part)
	except ValueError as exc:
		raise OperationError("invalid_cursor", "the pagination cursor is malformed") from exc


async def history(
	engine: AsyncEngine,
	registry: AsyncEngine,
	*,
	wallet_id: int | None = None,
	network: str | None = None,
	asset: str | None = None,
	status: str = API_STATUS_ALL,
	limit: int = DEFAULT_HISTORY_LIMIT,
	cursor: tuple[int, int] | None = None,
) -> HistoryPage:
	"""Incoming operations across every wallet, newest first, page by page.

	Пагинация курсорная («показать ещё», решение владельца): курсор —
	позиция последней выданной строки в порядке (блок ↓, ид ↓); следующая
	страница продолжает строго за ним, поэтому появление новых операций
	сверху не сдвигает уже показанное.

	Raises:
		OperationError: invalid_status / invalid_limit.
	"""
	if status not in HISTORY_STATUS_FILTERS:
		raise OperationError(
			"invalid_status", f"status must be one of {', '.join(HISTORY_STATUS_FILTERS)}"
		)
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")

	address_to_wallet, wallet_names = await user_views.wallet_display_map(engine)
	rows = await user_views.list_incoming(engine, before=cursor)
	infos = await _asset_infos(registry, {row.asset_id for row in rows})

	result: list[HistoryRow] = []
	next_cursor: tuple[int, int] | None = None
	for row in rows:
		info = infos.get(row.asset_id)
		if info is None:
			continue
		row_wallet_id = address_to_wallet.get(row.address)
		entry = HistoryRow(
			time=row.tx_time.isoformat(sep=" ", timespec="minutes") if row.tx_time else None,
			wallet_id=row_wallet_id,
			wallet=wallet_names.get(row_wallet_id, "—"),
			network=info["network"],
			asset=info["symbol"],
			amount=format_amount(int(row.amount), info["decimals"]),
			txid=row.txid,
			status=classify_transaction(row.status, row.balance_applied_at),
		)
		if wallet_id is not None and entry.wallet_id != wallet_id:
			continue
		if network is not None and entry.network != network:
			continue
		if asset is not None and entry.asset != asset:
			continue
		if status != API_STATUS_ALL and entry.status != status:
			continue
		result.append(entry)
		if limit and len(result) >= limit:
			# Лимит выбран не до конца выборки — есть следующая страница.
			next_cursor = (row.block_number, row.id)
			break
	return HistoryPage(rows=result, next_cursor=next_cursor)


@dataclass(frozen=True)
class Overview:
	"""The dashboard summary."""

	wallets: int
	applications: int
	addresses: int
	receipts: list[dict]
	recent: list[HistoryRow]


async def overview(engine: AsyncEngine, registry: AsyncEngine) -> Overview:
	"""Counters, receipts per network+asset and the freshest operations."""
	wallet_count, app_count, address_count = await user_views.overview_counters(engine)
	balance_rows = await user_views.list_balances(engine)
	pending_rows = await user_views.pending_incoming(engine)

	asset_ids = {row.asset_id for row in balance_rows} | {row.asset_id for row in pending_rows}
	infos = await _asset_infos(registry, asset_ids)

	totals: dict[int, dict[str, int]] = {}
	for row in balance_rows:
		entry = totals.setdefault(row.asset_id, {"received": 0, "pending": 0})
		entry["received"] += int(row.total_received)
	for row in pending_rows:
		entry = totals.setdefault(row.asset_id, {"received": 0, "pending": 0})
		entry["pending"] += int(row.amount)

	receipts = []
	for asset_id, entry in totals.items():
		info = infos.get(asset_id)
		if info is None:
			continue
		receipts.append(
			{
				"network": info["network"],
				"asset": info["symbol"],
				"received": format_amount(entry["received"], info["decimals"]),
				"pending": format_amount(entry["pending"], info["decimals"]),
			}
		)
	receipts.sort(key=lambda r: (r["network"], r["asset"]))

	recent = await history(engine, registry, limit=DEFAULT_RECENT_LIMIT)
	return Overview(
		wallets=wallet_count,
		applications=app_count,
		addresses=address_count,
		receipts=receipts,
		recent=recent.rows,
	)
