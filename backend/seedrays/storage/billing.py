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
	invoice_payments,
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


async def mark_overdue(engine: AsyncEngine, *, now: datetime) -> list[tuple[int, int]]:
	"""Move issued invoices past their deadline into the overdue state.

	Returns:
		``(invoice id, user id)`` pairs just moved — the caller suspends those
		users and writes the journal lines.
	"""
	condition = (invoices.c.state == STATE_ISSUED, invoices.c.due_at < now)
	async with engine.begin() as conn:
		rows = (
			await conn.execute(select(invoices.c.id, invoices.c.user_id).where(*condition))
		).all()
		if rows:
			await conn.execute(update(invoices).where(*condition).values(state=STATE_OVERDUE))
	return [(row.id, row.user_id) for row in rows]


async def has_unpaid(engine: AsyncEngine, user_id: int) -> bool:
	"""Whether the user still has an invoice awaiting money."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(invoices.c.id).where(
					invoices.c.user_id == user_id,
					invoices.c.state.in_((STATE_ISSUED, STATE_OVERDUE)),
				)
			)
		).first()
	return row is not None


async def access_state(engine: AsyncEngine, user_id: int) -> str:
	"""The user's billing access state; ``ok`` until something suspends them.

	Read on every Application API call and every cabinet request, so it stays
	a single point read by the primary key.
	"""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(user_billing.c.state).where(user_billing.c.user_id == user_id)
			)
		).first()
	return ACCESS_OK if row is None else row.state


async def set_access_state(
	engine: AsyncEngine, *, user_id: int, state: str, suspended_at: datetime | None
) -> None:
	"""Suspend the user for non-payment, or let them back in."""
	async with engine.begin() as conn:
		await upsert(
			conn,
			user_billing,
			{"user_id": user_id},
			{"state": state, "suspended_at": suspended_at},
		)


async def get_user_billing(engine: AsyncEngine, user_id: int):
	"""The user's billing state row, or None before anything was set."""
	async with engine.connect() as conn:
		return (
			await conn.execute(select(user_billing).where(user_billing.c.user_id == user_id))
		).first()


@dataclass(frozen=True)
class UnpaidInvoice:
	"""An invoice awaiting payment, with the address it is paid to."""

	id: int
	user_id: int
	network: str
	address: str
	amount: int
	credited: int
	state: str
	checked_at: datetime | None


async def list_unpaid(engine: AsyncEngine) -> list[UnpaidInvoice]:
	"""Invoices still awaiting money, oldest first — the payment check's work list.

	Only these addresses are polled: an address whose invoices are all settled
	costs nothing to leave alone until its user is billed again.
	"""
	query = (
		select(
			invoices.c.id,
			invoices.c.user_id,
			invoices.c.network,
			invoices.c.address,
			invoices.c.amount,
			invoices.c.credited,
			invoices.c.state,
			invoice_addresses.c.checked_at,
		)
		.join(
			invoice_addresses,
			(invoice_addresses.c.user_id == invoices.c.user_id)
			& (invoice_addresses.c.network == invoices.c.network),
			isouter=True,
		)
		.where(invoices.c.state.in_((STATE_ISSUED, STATE_OVERDUE)))
		.order_by(invoices.c.period_start, invoices.c.id)
	)
	async with engine.connect() as conn:
		rows = (await conn.execute(query)).all()
	return [
		UnpaidInvoice(
			id=row.id,
			user_id=row.user_id,
			network=row.network,
			address=row.address,
			amount=int(row.amount),
			credited=int(row.credited),
			state=row.state,
			checked_at=row.checked_at,
		)
		for row in rows
	]


async def set_address_checked(
	engine: AsyncEngine, *, user_id: int, network: str, checked_at: datetime
) -> None:
	"""Move the address's check cursor forward."""
	async with engine.begin() as conn:
		await conn.execute(
			update(invoice_addresses)
			.where(
				invoice_addresses.c.user_id == user_id,
				invoice_addresses.c.network == network,
			)
			.values(checked_at=checked_at)
		)


# Исходы записи наблюдённого платежа. Третий существует потому, что
# оборванный ответ провайдера мог отдать не все переводы транзакции:
# сумма уточняется вверх, вниз — никогда, иначе короткий ответ обесценил
# бы уже зачтённые деньги.
PAYMENT_NEW = "new"
PAYMENT_EXTENDED = "extended"
PAYMENT_KNOWN = "known"


async def record_payment(
	engine: AsyncEngine,
	*,
	network: str,
	address: str,
	txid: str,
	asset_id: int,
	amount: int,
	value: int,
	tx_time: datetime | None,
	finalized_at: datetime,
	event_index: int = 0,
) -> str:
	"""Record one observed transfer on an invoice address; idempotent.

	Only finalized transfers reach this function — the billing check asks the
	provider for confirmed ones only (ADR-0027), so there is no provisional
	state here and nothing to withdraw later.

	Args:
		engine: The billing database engine.
		network: Network the transfer happened in.
		address: The invoice address it landed on.
		txid: Transaction id.
		asset_id: Registry asset id of the asset that arrived.
		amount: Integer amount in the asset's minimal units.
		value: The same amount in micro-USDT, or zero when the asset is not
			one the gateway accepts as payment.
		tx_time: Block time, naive UTC.
		finalized_at: When the gateway saw it confirmed, naive UTC.
		event_index: Ordinal of the transfer inside the transaction.

	Returns:
		``PAYMENT_NEW`` when a row was recorded, ``PAYMENT_EXTENDED`` when a
		known row's amount was raised to a larger one just observed, and
		``PAYMENT_KNOWN`` when nothing changed.
	"""
	try:
		async with engine.begin() as conn:
			await conn.execute(
				insert(invoice_payments).values(
					network=network,
					address=address,
					txid=txid,
					asset_id=asset_id,
					event_index=event_index,
					amount=str(amount),
					value=str(value),
					tx_time=tx_time,
					finalized_at=finalized_at,
				)
			)
	except IntegrityError as exc:
		if unique_violation(exc) is None:
			raise  # не ключ идемпотентности — настоящая ошибка целостности
		async with engine.begin() as conn:
			known = (
				await conn.execute(
					select(invoice_payments).where(
						invoice_payments.c.txid == txid,
						invoice_payments.c.address == address,
						invoice_payments.c.asset_id == asset_id,
						invoice_payments.c.event_index == event_index,
					)
				)
			).first()
			if known is None or int(known.amount) >= amount:
				return PAYMENT_KNOWN
			await conn.execute(
				update(invoice_payments)
				.where(invoice_payments.c.id == known.id)
				.values(amount=str(amount), value=str(value))
			)
		return PAYMENT_EXTENDED
	return PAYMENT_NEW


async def creditable_payments(engine: AsyncEngine, *, address: str) -> list:
	"""Payments on one address with money still not credited, oldest first.

	Читается по адресу, а не по счёту: адрес постоянный, а счета сменяют друг
	друга, и незачтённый остаток прошлого платежа — это аванс, который должен
	достаться следующему счёту.
	"""
	query = (
		select(
			invoice_payments.c.id,
			invoice_payments.c.value,
			invoice_payments.c.credited,
		)
		.where(
			invoice_payments.c.address == address,
			# Нулевая оценка — чужой актив: деньгами счёта он не является.
			invoice_payments.c.value != "0",
			invoice_payments.c.value != invoice_payments.c.credited,
		)
		.order_by(invoice_payments.c.tx_time, invoice_payments.c.id)
	)
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


async def apply_credits(
	engine: AsyncEngine,
	*,
	invoice_id: int,
	allocations: list[tuple[int, int]],
	credited_total: int,
	paid_at: datetime | None,
) -> None:
	"""Credit payments to an invoice; the invoice and the payments move together.

	The whole allocation lands in one database transaction: a credited payment
	without the matching invoice state — or the reverse — would be money the
	gateway cannot account for.

	Args:
		engine: The billing database engine.
		invoice_id: The invoice being settled.
		allocations: ``(payment id, its new credited total)`` pairs.
		credited_total: The invoice's new credited total.
		paid_at: Settlement time when the invoice is now paid; None while it
			is still short.
	"""
	async with engine.begin() as conn:
		for payment_id, credited in allocations:
			await conn.execute(
				update(invoice_payments)
				.where(invoice_payments.c.id == payment_id)
				.values(credited=str(credited), invoice_id=invoice_id)
			)
		values: dict = {"credited": str(credited_total)}
		if paid_at is not None:
			values["state"] = STATE_PAID
			values["paid_at"] = paid_at
		await conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(**values))


async def invoice_exists(engine: AsyncEngine, *, user_id: int, period_start: datetime) -> bool:
	"""Whether this user's period is already billed (the pass skips it then)."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(invoices.c.id).where(
					invoices.c.user_id == user_id,
					invoices.c.period_start == period_start,
				)
			)
		).first()
	return row is not None


async def get_invoice(engine: AsyncEngine, invoice_id: int):
	"""One invoice row by id, or None."""
	async with engine.connect() as conn:
		return (await conn.execute(select(invoices).where(invoices.c.id == invoice_id))).first()


async def mark_paid_manually(
	engine: AsyncEngine,
	*,
	invoice_id: int,
	operator_id: int,
	reason: str,
	paid_at: datetime,
	amount: int,
) -> None:
	"""Settle an invoice by the operator's word: money that arrived outside the gateway.

	The invoice carries who confirmed it and why — that is the only trace such
	a payment leaves, since it has neither a transaction nor an asset behind it.
	"""
	async with engine.begin() as conn:
		await conn.execute(
			update(invoices)
			.where(invoices.c.id == invoice_id)
			.values(
				state=STATE_PAID,
				paid_at=paid_at,
				credited=str(amount),
				manual_operator_id=operator_id,
				manual_reason=reason,
			)
		)
