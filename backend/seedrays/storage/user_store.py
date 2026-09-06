"""User-database operations: bindings, on-chain transactions, balance application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import unique_violation
from seedrays.storage.schema_user import balances, bindings, transactions

# Допустимые доменные значения строк транзакций (ADR-0017). Проверяются до
# вставки: на финансовом пути записи нарушение CHECK-ограничения не должно
# быть неотличимо от конфликта ключа идемпотентности.
DIRECTIONS = ("in", "out")
STATUSES = ("success", "failed")


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
	event_index: int = 0,
	finalized_at: datetime | None = None,
) -> bool:
	"""Record one observed on-chain transaction; idempotent (ADR-0017, ADR-0021).

	A row observed above the finality boundary is provisional
	(``finalized_at`` is None) and may later be removed by
	:func:`delete_unfinalized` if the chain reorganizes. The authoritative
	scan of the finalized zone records the same key with ``finalized_at``
	set; an existing provisional row is then promoted in place (its block,
	time and status are refreshed — a reorganization may have moved it).

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
		event_index: Ordinal of the transfer inside the transaction.
		finalized_at: Naive-UTC finalization marker; None — provisional.

	Returns:
		True if a new row was inserted, False if the key already existed
		(including the promotion of a provisional row).

	Raises:
		ValueError: On a domain value outside the allowed sets.
		IntegrityError: On an integrity violation other than the
			idempotency key — such a row must never be dropped silently.
	"""
	if direction not in DIRECTIONS:
		raise ValueError(f"invalid transaction direction {direction!r} for tx {txid}")
	if status not in STATUSES:
		raise ValueError(f"invalid transaction status {status!r} for tx {txid}")
	try:
		async with engine.begin() as conn:
			await conn.execute(
				insert(transactions).values(
					address=address,
					txid=txid,
					asset_id=asset_id,
					direction=direction,
					event_index=event_index,
					amount=str(amount),
					block_number=block_number,
					tx_time=tx_time,
					status=status,
					finalized_at=finalized_at,
				)
			)
	except IntegrityError as exc:
		if unique_violation(exc) is None:
			raise  # не ключ идемпотентности — настоящая ошибка целостности
		if finalized_at is not None:
			# Продвижение предварительной строки: авторитетное наблюдение
			# уточняет блок/время/статус и ставит отметку финализации.
			async with engine.begin() as conn:
				await conn.execute(
					update(transactions)
					.where(
						transactions.c.txid == txid,
						transactions.c.address == address,
						transactions.c.asset_id == asset_id,
						transactions.c.direction == direction,
						transactions.c.event_index == event_index,
						transactions.c.finalized_at.is_(None),
					)
					.values(
						block_number=block_number,
						tx_time=tx_time,
						status=status,
						finalized_at=finalized_at,
					)
				)
		return False
	return True


async def apply_finalized(
	engine: AsyncEngine,
	*,
	asset_ids: set[int],
	applied_at: datetime,
) -> int:
	"""Apply finalized successful rows to the balance cache; idempotent.

	A row is applied when it was confirmed by the authoritative scan of the
	finalized zone (``finalized_at`` is set, ADR-0021), its execution
	succeeded and it has not been applied before. The balance update and
	the row marker land in one database transaction (ADR-0017).

	Args:
		engine: The owner's user-database engine.
		asset_ids: Assets of the network being applied.
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
					transactions.c.finalized_at.is_not(None),
					transactions.c.status == "success",
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
				# Максимум, а не последняя строка выборки: порядок выдачи
				# СУБД не обязан совпадать с порядком блоков.
				if row.tx_time is not None and (
					last_deposit is None or row.tx_time > last_deposit
				):
					last_deposit = row.tx_time
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


async def delete_unfinalized(
	engine: AsyncEngine, *, asset_ids: set[int], up_to_block: int
) -> list[str]:
	"""Remove provisional rows the finalized chain never confirmed (ADR-0021).

	Called after the authoritative scan of the finalized zone: a provisional
	row whose block is already inside that zone but which the scan did not
	promote was reorganized out of the chain — keeping it would show the
	user a payment that no longer exists.

	Args:
		engine: The owner's user-database engine.
		asset_ids: Assets of the network being cleaned.
		up_to_block: Upper bound of the authoritatively scanned zone.

	Returns:
		Transaction ids of the removed rows (for the caller's log).
	"""
	if not asset_ids:
		return []
	condition = (
		transactions.c.finalized_at.is_(None),
		transactions.c.balance_applied_at.is_(None),
		transactions.c.block_number <= up_to_block,
		transactions.c.asset_id.in_(asset_ids),
	)
	async with engine.begin() as conn:
		rows = (await conn.execute(select(transactions.c.txid).where(*condition))).all()
		if rows:
			await conn.execute(delete(transactions).where(*condition))
	return [row.txid for row in rows]
