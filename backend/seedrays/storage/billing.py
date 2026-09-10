"""Billing database operations: master wallets, invoice addresses, invoices.

The billing counterpart of :mod:`seedrays.storage.registry` — the only place
that sees SQL of the owner's billing data (ADR-0006). Every datetime follows
the storage-layer convention: naive UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import unique_violation, upsert
from seedrays.storage.schema_billing import (
	invoice_addresses,
	invoices,
	master_wallets,
	user_billing,
)

# Состояния счёта (ADR-0027) — единственная точка правды для сравнений.
STATE_ISSUED = "issued"
STATE_PAID = "paid"
STATE_OVERDUE = "overdue"

# Состояния доступа по биллингу.
ACCESS_OK = "ok"
ACCESS_SUSPENDED = "suspended"


@dataclass(frozen=True)
class MasterWallet:
	"""The owner's watch-only wallet of one payment network."""

	network: str
	xpub: str
	xpub_hash: str


def _master(row) -> MasterWallet:
	return MasterWallet(network=row.network, xpub=row.xpub, xpub_hash=row.xpub_hash)


async def get_master_wallet(engine: AsyncEngine, network: str) -> MasterWallet | None:
	"""The owner's master wallet of one network; None when it is not set up."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(master_wallets).where(master_wallets.c.network == network)
			)
		).first()
	return None if row is None else _master(row)


async def list_master_wallets(engine: AsyncEngine) -> list[MasterWallet]:
	"""Every master wallet, network order — the panel's list and the payment choice."""
	async with engine.connect() as conn:
		rows = (
			await conn.execute(select(master_wallets).order_by(master_wallets.c.network))
		).all()
	return [_master(row) for row in rows]


async def find_master_wallet_by_hash(
	engine: AsyncEngine, xpub_hash: str
) -> MasterWallet | None:
	"""The master wallet carrying a key fingerprint, or None.

	The gateway-side half of the "one xpub — one wallet" rule: a user attaching
	a wallet is checked against this, and the owner adding a master wallet is
	checked against the registry index (ADR-0027).
	"""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(master_wallets).where(master_wallets.c.xpub_hash == xpub_hash)
			)
		).first()
	return None if row is None else _master(row)


async def set_master_wallet(
	engine: AsyncEngine, *, network: str, xpub: str, xpub_hash: str
) -> None:
	"""Create or replace the master wallet of one network.

	Проверка «ключ уже служит другой сети» делается явно, а не ловлей
	нарушения ключа из :func:`upsert`: тот трактует любой уникальный конфликт
	как проигранную гонку вставки и повторяет запись — правильно для своей
	задачи, но здесь это проглотило бы отказ.

	Raises:
		ValueError: If the fingerprint already belongs to another network's
			master wallet — one key must not serve two networks.
	"""
	taken = await find_master_wallet_by_hash(engine, xpub_hash)
	if taken is not None and taken.network != network:
		raise ValueError("this xpub is already a master wallet of another network")
	try:
		async with engine.begin() as conn:
			await upsert(
				conn,
				master_wallets,
				{"network": network},
				{"xpub": xpub, "xpub_hash": xpub_hash},
			)
	except IntegrityError as exc:
		if unique_violation(exc) != "master_wallets.xpub_hash":
			raise  # иная ошибка целостности — не «ключ уже занят»
		# Гонка с внесением того же ключа в другую сеть: уникальный ключ
		# схемы — страховка на случай, если проверка выше не успела.
		raise ValueError("this xpub is already a master wallet of another network") from exc


async def delete_master_wallet(engine: AsyncEngine, network: str) -> None:
	"""Drop the master wallet of one network."""
	async with engine.begin() as conn:
		await conn.execute(delete(master_wallets).where(master_wallets.c.network == network))


async def get_invoice_address(engine: AsyncEngine, *, user_id: int, network: str):
	"""The user's permanent invoice address in one network, or None."""
	async with engine.connect() as conn:
		return (
			await conn.execute(
				select(invoice_addresses).where(
					invoice_addresses.c.user_id == user_id,
					invoice_addresses.c.network == network,
				)
			)
		).first()


async def next_invoice_index(engine: AsyncEngine, network: str) -> int:
	"""The next free derivation index of the master wallet of one network."""
	async with engine.connect() as conn:
		highest = (
			await conn.execute(
				select(func.max(invoice_addresses.c.derivation_index)).where(
					invoice_addresses.c.network == network
				)
			)
		).scalar()
	return 0 if highest is None else highest + 1


async def insert_invoice_address(
	engine: AsyncEngine,
	*,
	user_id: int,
	network: str,
	address: str,
	derivation_index: int,
) -> None:
	"""Insert one invoice address.

	Raises:
		IntegrityError: On a uniqueness conflict — the caller resolves it
			(index allocation is serialized above this layer).
	"""
	async with engine.begin() as conn:
		await conn.execute(
			insert(invoice_addresses).values(
				user_id=user_id,
				network=network,
				address=address,
				derivation_index=derivation_index,
			)
		)


async def insert_invoice(
	engine: AsyncEngine,
	*,
	user_id: int,
	period_start: datetime,
	period_end: datetime,
	turnover: int,
	rate_percent: str,
	threshold: int,
	amount: int,
	due_at: datetime,
	network: str,
	address: str,
) -> int | None:
	"""Issue one invoice; returns its id, or None when the period is already billed.

	Idempotency rests on the "user + period" key of the schema: a second
	billing pass over the same period changes nothing, whatever the reason it
	ran twice.

	Args:
		engine: The billing database engine.
		user_id: The user being billed.
		period_start: First instant of the period, naive UTC.
		period_end: First instant of the next period, naive UTC.
		turnover: The period's turnover in micro-USDT.
		rate_percent: The rate applied, as the operator typed it.
		threshold: The threshold applied, in micro-USDT.
		amount: The amount due, in micro-USDT.
		due_at: Payment deadline, naive UTC.
		network: Payment network of the invoice.
		address: Payment address of the invoice.

	Returns:
		The new invoice id, or None if this user's period already has one.
	"""
	try:
		async with engine.begin() as conn:
			result = await conn.execute(
				insert(invoices).values(
					user_id=user_id,
					period_start=period_start,
					period_end=period_end,
					turnover=str(turnover),
					rate_percent=rate_percent,
					threshold=str(threshold),
					amount=str(amount),
					due_at=due_at,
					network=network,
					address=address,
				)
			)
	except IntegrityError as exc:
		if unique_violation(exc) is None:
			raise  # не ключ идемпотентности — настоящая ошибка целостности
		return None
	return result.inserted_primary_key[0]


async def list_invoices(
	engine: AsyncEngine, *, user_id: int | None = None, state: str | None = None
) -> list:
	"""Invoice rows, newest period first; optionally scoped to a user or a state."""
	query = select(invoices).order_by(invoices.c.period_start.desc(), invoices.c.id.desc())
	if user_id is not None:
		query = query.where(invoices.c.user_id == user_id)
	if state is not None:
		query = query.where(invoices.c.state == state)
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


async def mark_overdue(engine: AsyncEngine, *, now: datetime) -> list[int]:
	"""Move issued invoices past their deadline into the overdue state.

	Returns:
		Ids of the invoices just moved — the caller suspends their users and
		writes the journal lines.
	"""
	condition = (invoices.c.state == STATE_ISSUED, invoices.c.due_at < now)
	async with engine.begin() as conn:
		rows = (await conn.execute(select(invoices.c.id).where(*condition))).all()
		if rows:
			await conn.execute(update(invoices).where(*condition).values(state=STATE_OVERDUE))
	return [row.id for row in rows]


async def get_user_billing(engine: AsyncEngine, user_id: int):
	"""The user's billing state row, or None before anything was set."""
	async with engine.connect() as conn:
		return (
			await conn.execute(select(user_billing).where(user_billing.c.user_id == user_id))
		).first()


async def set_payment_network(engine: AsyncEngine, *, user_id: int, network: str) -> None:
	"""Store the user's payment network choice."""
	async with engine.begin() as conn:
		await upsert(conn, user_billing, {"user_id": user_id}, {"network": network})
