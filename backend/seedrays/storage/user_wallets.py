"""User-database operations: wallets (the watch-only xpub rows)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.schema_user import bindings, wallets


@dataclass(frozen=True)
class WalletRecord:
	"""One wallet of the user, with its bound address count."""

	id: int
	family: str
	xpub: str
	label: str
	created_at: datetime | None
	addresses: int


async def list_wallets(engine: AsyncEngine) -> list[WalletRecord]:
	"""The user's wallets with per-wallet bound address counts."""
	async with engine.connect() as conn:
		rows = (await conn.execute(select(wallets).order_by(wallets.c.id))).all()
		counts = dict(
			(
				await conn.execute(
					select(bindings.c.wallet_id, func.count())
					.group_by(bindings.c.wallet_id)
				)
			).all()
		)
	return [
		WalletRecord(
			id=row.id,
			family=row.family,
			xpub=row.xpub,
			label=row.label,
			created_at=row.created_at,
			addresses=counts.get(row.id, 0),
		)
		for row in rows
	]


async def add_wallet(engine: AsyncEngine, *, family: str, xpub: str, label: str) -> int:
	"""Insert one wallet row; returns its id."""
	async with engine.begin() as conn:
		result = await conn.execute(
			insert(wallets).values(family=family, xpub=xpub, label=label)
		)
		return result.inserted_primary_key[0]


async def get_wallet(engine: AsyncEngine, wallet_id: int):
	"""One wallet row by id, or None."""
	async with engine.connect() as conn:
		return (await conn.execute(select(wallets).where(wallets.c.id == wallet_id))).first()
