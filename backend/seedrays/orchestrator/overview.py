"""Cabinet read operations: incoming history and the dashboard summary.

Amounts leave this module as exact decimal strings computed with integer
arithmetic from the minimal units (never floats) — the same principle as
the Application API (ADR-0011).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.schema_registry import assets
from seedrays.storage.schema_user import applications, balances, bindings, transactions, wallets

HISTORY_LIMIT_DEFAULT = 50
_STATUSES = ("confirmed", "pending", "failed", "all")


def format_amount(minimal_units: int, decimals: int) -> str:
	"""An exact decimal string from minimal units; trailing zeros trimmed."""
	if decimals <= 0:
		return str(minimal_units)
	sign = "-" if minimal_units < 0 else ""
	whole, fraction = divmod(abs(minimal_units), 10**decimals)
	tail = str(fraction).rjust(decimals, "0").rstrip("0")
	return f"{sign}{whole}.{tail}" if tail else f"{sign}{whole}"


async def _asset_infos(registry: AsyncEngine, asset_ids: set[int]) -> dict[int, dict]:
	if not asset_ids:
		return {}
	async with registry.connect() as conn:
		rows = (await conn.execute(select(assets).where(assets.c.id.in_(asset_ids)))).all()
	return {
		row.id: {"network": row.network, "symbol": row.symbol, "decimals": row.decimals}
		for row in rows
	}


async def _wallet_by_address(engine: AsyncEngine) -> tuple[dict[str, int], dict[int, str]]:
	"""(address → wallet id, wallet id → display name)."""
	async with engine.connect() as conn:
		binding_rows = (
			await conn.execute(select(bindings.c.address, bindings.c.wallet_id))
		).all()
		wallet_rows = (await conn.execute(select(wallets))).all()
	names = {w.id: (w.label or w.family.upper()) for w in wallet_rows}
	return {b.address: b.wallet_id for b in binding_rows}, names


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


async def history(
	engine: AsyncEngine,
	registry: AsyncEngine,
	*,
	wallet_id: int | None = None,
	network: str | None = None,
	asset: str | None = None,
	status: str = "all",
	limit: int = HISTORY_LIMIT_DEFAULT,
) -> list[HistoryRow]:
	"""Incoming operations across every wallet, newest first.

	Raises:
		OperationError: invalid_status / invalid_limit.
	"""
	from seedrays.orchestrator.operations import OperationError

	if status not in _STATUSES:
		raise OperationError("invalid_status", f"status must be one of {', '.join(_STATUSES)}")
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")

	address_to_wallet, wallet_names = await _wallet_by_address(engine)
	async with engine.connect() as conn:
		rows = (
			await conn.execute(
				select(transactions)
				.where(transactions.c.direction == "in")
				.order_by(transactions.c.block_number.desc(), transactions.c.id.desc())
			)
		).all()
	infos = await _asset_infos(registry, {row.asset_id for row in rows})

	result: list[HistoryRow] = []
	for row in rows:
		info = infos.get(row.asset_id)
		if info is None:
			continue
		if row.status == "failed":
			row_status = "failed"
		elif row.balance_applied_at is not None:
			row_status = "confirmed"
		else:
			row_status = "pending"
		row_wallet_id = address_to_wallet.get(row.address)
		entry = HistoryRow(
			time=row.tx_time.isoformat(sep=" ", timespec="minutes") if row.tx_time else None,
			wallet_id=row_wallet_id,
			wallet=wallet_names.get(row_wallet_id, "—"),
			network=info["network"],
			asset=info["symbol"],
			amount=format_amount(int(row.amount), info["decimals"]),
			txid=row.txid,
			status=row_status,
		)
		if wallet_id is not None and entry.wallet_id != wallet_id:
			continue
		if network is not None and entry.network != network:
			continue
		if asset is not None and entry.asset != asset:
			continue
		if status != "all" and entry.status != status:
			continue
		result.append(entry)
		if limit and len(result) >= limit:
			break
	return result


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
	async with engine.connect() as conn:
		wallet_count = (await conn.execute(select(func.count()).select_from(wallets))).scalar()
		app_count = (
			await conn.execute(select(func.count()).select_from(applications))
		).scalar()
		address_count = (
			await conn.execute(select(func.count()).select_from(bindings))
		).scalar()
		balance_rows = (await conn.execute(select(balances))).all()
		pending_rows = (
			await conn.execute(
				select(transactions.c.asset_id, transactions.c.amount).where(
					transactions.c.direction == "in",
					transactions.c.status == "success",
					transactions.c.balance_applied_at.is_(None),
				)
			)
		).all()

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

	recent = await history(engine, registry, limit=5)
	return Overview(
		wallets=wallet_count or 0,
		applications=app_count or 0,
		addresses=address_count or 0,
		receipts=receipts,
		recent=recent,
	)
