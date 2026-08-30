"""User-database operations: bindings, on-chain transactions, balance application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.schema_user import balances, bindings, transactions


@dataclass(frozen=True)
class BindingAddress:
	"""One tracked address of a user: an entry of the watcher's match filter."""

	network: str
	address: str
	memo: str


async def list_binding_addresses(engine: AsyncEngine) -> list[BindingAddress]:
	"""Return every bound address of one user database."""
	async with engine.connect() as conn:
		rows = (
			await conn.execute(select(bindings.c.network, bindings.c.address, bindings.c.memo))
		).all()
	return [BindingAddress(network=r.network, address=r.address, memo=r.memo) for r in rows]


async def record_transaction(
	engine: AsyncEngine,
	*,
	address: str,
	txid: str,
	asset_id: int,
	direction: str,
	amount: int,
	block_number: int,
	tx_time: datetime | None,
	status: str,
) -> bool:
	"""Insert one observed on-chain transaction; idempotent.

	Args:
		engine: The owner's user-database engine.
		address: The bound address the transfer touches.
		txid: Transaction id in the network.
		asset_id: Registry asset id (cross-database reference, ADR-0010).
		direction: ``in`` or ``out`` relative to the address.
		amount: Integer amount in the asset's minimal units.
		block_number: Block the transaction was included in.
		tx_time: Block time, naive UTC.
		status: Execution outcome: ``success`` or ``failed``.

	Returns:
		True if the row was inserted, False if it already existed.
	"""
	try:
		async with engine.begin() as conn:
			await conn.execute(
				insert(transactions).values(
					address=address,
					txid=txid,
					asset_id=asset_id,
					direction=direction,
					amount=str(amount),
					block_number=block_number,
					tx_time=tx_time,
					status=status,
				)
			)
	except IntegrityError:
		return False  # уже видели: уникальный ключ «транзакция + адрес + актив»
	return True


async def apply_finalized(
	engine: AsyncEngine,
	*,
	asset_ids: set[int],
	boundary_block: int,
	applied_at: datetime,
) -> int:
	"""Apply finalized successful rows to the balance cache; idempotent.

	A row is applied when its block is at or below the finality boundary,
	its execution succeeded and it has not been applied before. The balance
	update and the row marker land in one database transaction (ADR-0017).

	Args:
		engine: The owner's user-database engine.
		asset_ids: Assets of the network the boundary belongs to.
		boundary_block: The network's finality boundary (block number).
		applied_at: Marker timestamp, naive UTC.

	Returns:
		The number of rows applied.
	"""
	if not asset_ids:
		return 0
	applied = 0
	async with engine.begin() as conn:
		rows = (
			await conn.execute(
				select(transactions).where(
					transactions.c.balance_applied_at.is_(None),
					transactions.c.status == "success",
					transactions.c.block_number <= boundary_block,
					transactions.c.asset_id.in_(asset_ids),
				)
			)
		).all()
		for row in rows:
			amount = int(row.amount)
			balance_row = (
				await conn.execute(
					select(balances).where(
						balances.c.address == row.address,
						balances.c.asset_id == row.asset_id,
					)
				)
			).first()
			if balance_row is None:
				current, received, last_deposit = 0, 0, None
			else:
				current, received = int(balance_row.balance), int(balance_row.total_received)
				last_deposit = balance_row.last_deposit_at
			if row.direction == "in":
				current += amount
				received += amount
				last_deposit = row.tx_time or last_deposit
			else:
				current -= amount
			if balance_row is None:
				await conn.execute(
					insert(balances).values(
						address=row.address,
						asset_id=row.asset_id,
						balance=str(current),
						total_received=str(received),
						last_deposit_at=last_deposit,
					)
				)
			else:
				await conn.execute(
					update(balances)
					.where(
						balances.c.address == row.address,
						balances.c.asset_id == row.asset_id,
					)
					.values(
						balance=str(current),
						total_received=str(received),
						last_deposit_at=last_deposit,
					)
				)
			await conn.execute(
				update(transactions)
				.where(transactions.c.id == row.id)
				.values(balance_applied_at=applied_at)
			)
			applied += 1
	return applied
