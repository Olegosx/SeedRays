"""User-database operations: bindings, on-chain transactions, balance application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import unique_violation
from seedrays.storage.schema_user import balances, bindings, transactions

# Доменные значения финансовой модели (ADR-0017) — единственная точка
# правды для сравнений и записи; голые литералы в потребителях запрещены.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUSES = (STATUS_SUCCESS, STATUS_FAILED)

# Статусы, которыми операция показывается потребителям (API и кабинет),
# и допустимые значения фильтра истории.
API_STATUS_CONFIRMED = "confirmed"
API_STATUS_PENDING = "pending"
API_STATUS_FAILED = "failed"
# Значение фильтра «не отбирать по статусу».
API_STATUS_ALL = "all"
HISTORY_STATUS_FILTERS = (
	API_STATUS_CONFIRMED,
	API_STATUS_PENDING,
	API_STATUS_FAILED,
	API_STATUS_ALL,
)


def classify_transaction(status: str, balance_applied_at: datetime | None) -> str:
	"""The consumer-facing status of one row (ADR-0017, single point of truth).

	``confirmed`` — успешна и учтена в балансе; ``pending`` — записана, но
	ещё не учтена (включая предварительные строки ADR-0021); ``failed`` —
	исполнение провалилось.
	"""
	if status == STATUS_FAILED:
		return API_STATUS_FAILED
	if balance_applied_at is not None:
		return API_STATUS_CONFIRMED
	return API_STATUS_PENDING


def history_status_clause(api_status: str):
	"""SQL condition selecting one consumer-facing status.

	Зеркало :func:`classify_transaction`: то же правило, выраженное для
	базы, чтобы отбор по статусу шёл запросом, а не перебором уже
	вычитанных строк. Держать их рядом обязательно — разъехавшись, они
	покажут пользователю одно, а отфильтруют другое.
	"""
	if api_status == API_STATUS_FAILED:
		return transactions.c.status == STATUS_FAILED
	if api_status == API_STATUS_CONFIRMED:
		return and_(
			transactions.c.status != STATUS_FAILED,
			transactions.c.balance_applied_at.is_not(None),
		)
	return and_(
		transactions.c.status != STATUS_FAILED,
		transactions.c.balance_applied_at.is_(None),
	)


@dataclass(frozen=True)
class AppliedRow:
	"""One transaction row just applied to the balance cache.

	Возвращается наружу, чтобы проход мог оставить след в журнале: без
	строки об учтённом поступлении вопрос «шлюз видел этот платёж и что с
	ним стало» не разобрать, не открывая базу владельца.
	"""

	txid: str
	address: str
	direction: str
	amount: int


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


async def get_binding(
	engine: AsyncEngine, *, network: str, application_id: int, app_user_id: int
):
	"""The binding row of one owner in one network, or None."""
	async with engine.connect() as conn:
		return (
			await conn.execute(
				select(bindings).where(
					bindings.c.network == network,
					bindings.c.application_id == application_id,
					bindings.c.app_user_id == app_user_id,
				)
			)
		).first()


async def reused_derivation_index(
	engine: AsyncEngine, *, wallet_id: int, application_id: int, app_user_id: int
) -> int | None:
	"""The owner's index in another network of the same wallet, if any.

	Переиспользование индекса — осознанное свойство модели: та же связка
	«кошелёк + приложение + пользователь приложения» в другой сети даёт
	плательщику тот же адрес (EVM-свойство).
	"""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(bindings.c.derivation_index)
				.where(
					bindings.c.wallet_id == wallet_id,
					bindings.c.application_id == application_id,
					bindings.c.app_user_id == app_user_id,
				)
				.limit(1)
			)
		).first()
	return None if row is None else row.derivation_index


async def next_derivation_index(engine: AsyncEngine, wallet_id: int) -> int:
	"""The wallet's next free derivation index."""
	async with engine.connect() as conn:
		max_index = (
			await conn.execute(
				select(func.max(bindings.c.derivation_index)).where(
					bindings.c.wallet_id == wallet_id
				)
			)
		).scalar()
	return 0 if max_index is None else max_index + 1


async def insert_binding(
	engine: AsyncEngine,
	*,
	wallet_id: int,
	network: str,
	address: str,
	application_id: int,
	app_user_id: int,
	derivation_index: int,
) -> None:
	"""Insert one binding row.

	Raises:
		IntegrityError: On a uniqueness conflict — the caller resolves it
			(the allocation is serialized above this layer).
	"""
	async with engine.begin() as conn:
		await conn.execute(
			insert(bindings).values(
				wallet_id=wallet_id,
				network=network,
				address=address,
				application_id=application_id,
				app_user_id=app_user_id,
				derivation_index=derivation_index,
			)
		)


async def list_bindings_of_app_user(
	engine: AsyncEngine, app_user_id: int, *, network: str | None = None
) -> list:
	"""Binding rows of one application user, optionally scoped to a network."""
	query = select(bindings).where(bindings.c.app_user_id == app_user_id)
	if network is not None:
		query = query.where(bindings.c.network == network)
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


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
	counterparty: str | None = None,
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
		counterparty: The other side of the transfer — the sender of an
			incoming row, the recipient of an outgoing one. None when the
			source does not report it.
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
					counterparty=counterparty,
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
						counterparty=counterparty,
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
) -> list[AppliedRow]:
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
		The rows applied, for the caller's log.
	"""
	if not asset_ids:
		return []
	applied: list[AppliedRow] = []
	async with engine.begin() as conn:
		rows = (
			await conn.execute(
				select(transactions).where(
					transactions.c.balance_applied_at.is_(None),
					transactions.c.finalized_at.is_not(None),
					transactions.c.status == STATUS_SUCCESS,
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
			if row.direction == DIRECTION_IN:
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
			applied.append(
				AppliedRow(
					txid=row.txid,
					address=row.address,
					direction=row.direction,
					amount=amount,
				)
			)
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
