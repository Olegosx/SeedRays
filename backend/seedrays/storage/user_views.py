"""User-database read operations shaped for screens and API read paths.

The «read operations per screen» part of the storage interface
(ADR-0006 addendum): each function serves one screen or one API read —
wide, purpose-named reads instead of generic query building. Status
semantics come from :mod:`seedrays.storage.user_store` — the single
point of truth of the financial model.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.schema_user import applications, balances, bindings, transactions, wallets
from seedrays.storage.user_store import DIRECTION_IN, STATUS_SUCCESS


async def overview_counters(engine: AsyncEngine) -> tuple[int, int, int]:
	"""(wallets, applications, bound addresses) for the dashboard."""
	async with engine.connect() as conn:
		wallet_count = (await conn.execute(select(func.count()).select_from(wallets))).scalar()
		app_count = (
			await conn.execute(select(func.count()).select_from(applications))
		).scalar()
		address_count = (
			await conn.execute(select(func.count()).select_from(bindings))
		).scalar()
	return wallet_count or 0, app_count or 0, address_count or 0


async def wallet_display_map(engine: AsyncEngine) -> tuple[dict[str, int], dict[int, str]]:
	"""(address → wallet id, wallet id → display name) for history rows."""
	async with engine.connect() as conn:
		binding_rows = (
			await conn.execute(select(bindings.c.address, bindings.c.wallet_id))
		).all()
		wallet_rows = (await conn.execute(select(wallets))).all()
	names = {w.id: (w.label or w.family.upper()) for w in wallet_rows}
	return {b.address: b.wallet_id for b in binding_rows}, names


async def list_incoming(engine: AsyncEngine, *, addresses: list[str] | None = None) -> list:
	"""Incoming transaction rows, newest first; optionally scoped to addresses.

	Кормит и историю кабинета (без охвата адресов), и историю API
	приложений (по адресам одного пользователя приложения).
	"""
	query = (
		select(transactions)
		.where(transactions.c.direction == DIRECTION_IN)
		.order_by(transactions.c.block_number.desc(), transactions.c.id.desc())
	)
	if addresses is not None:
		query = query.where(transactions.c.address.in_(addresses))
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


async def pending_incoming(engine: AsyncEngine, *, addresses: list[str] | None = None) -> list:
	"""Successful incoming rows not applied to balances yet (the pending sums)."""
	query = select(
		transactions.c.address, transactions.c.asset_id, transactions.c.amount
	).where(
		transactions.c.direction == DIRECTION_IN,
		transactions.c.status == STATUS_SUCCESS,
		transactions.c.balance_applied_at.is_(None),
	)
	if addresses is not None:
		query = query.where(transactions.c.address.in_(addresses))
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


async def list_balances(engine: AsyncEngine, *, addresses: list[str] | None = None) -> list:
	"""Balance rows, optionally scoped to addresses."""
	query = select(balances)
	if addresses is not None:
		query = query.where(balances.c.address.in_(addresses))
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()
