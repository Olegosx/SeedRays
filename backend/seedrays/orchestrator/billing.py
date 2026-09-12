"""Billing operations: the fee the gateway owner takes from users (ADR-0027).

This module owns the turnover side of the fee: what a user earned over a
period and how that is valued. Issuing invoices, crediting payments and
suspending access build on it in their own modules.

The accounting unit is micro-USDT — an integer of the sixth decimal place,
the way USDT itself is denominated in TRON. Everything stays in integers:
a payment gateway may never compute money with floating point.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains
from seedrays.chains.base import ChainDataSourceError, Direction, TransferStatus
from seedrays.derivation.derive import InvalidKeyError, PrivateKeyError, derive_address
from seedrays.orchestrator.money import format_amount
from seedrays.orchestrator.operations import OperationError
from seedrays.orchestrator.seclog import ACTOR_SYSTEM, OUTCOME_SUCCESS, SecurityLog
from seedrays.storage import billing as billing_store
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_views
from seedrays.storage.registry import KIND_NATIVE, KIND_TOKEN
from seedrays.storage.engine import (
	billing_db_path,
	create_sqlite_engine,
	naive_utc,
	now_utc,
	registry_db_path,
	user_db_path,
)

logger = logging.getLogger(__name__)

# Единица учёта вознаграждения: целое в шестом знаке после запятой.
USDT_DECIMALS = 6
MICRO_USDT = 10**USDT_DECIMALS

# Сеть оплаты по умолчанию: пустое значение в состоянии биллинга означает
# именно её (ADR-0027). Живёт в коде, а не в схеме — сменить умолчание
# миграцией было бы странно.
DEFAULT_PAYMENT_NETWORK = "tron"

# Настройка реестра: JSON-список адресов контрактов, засчитываемых в оборот
# по одной сети. Символам активов доверия нет — каталог пополняется всем, что
# пришло на адрес, и поддельный «USDT» раздул бы оборот (ADR-0027).
SETTING_ASSETS_PREFIX = "billing.assets."

# Условия вознаграждения — настройки реестра (ADR-0016), страница панели.
# Выключено по умолчанию: установка не должна начать выставлять счета сама.
SETTING_ENABLED = "billing.enabled"
SETTING_RATE = "billing.rate_percent"
SETTING_THRESHOLD = "billing.threshold_usdt"
SETTING_DUE_DAYS = "billing.due_days"

# Активы, которыми принимается оплата счетов, по сетям: JSON-список адресов
# контрактов. Всё остальное, пришедшее на адрес счёта, — чужой актив.
SETTING_PAYMENT_ASSETS_PREFIX = "billing.payment_assets."
# Допуск недоплаты в процентах от суммы счёта: перевод, недошедший на копейку
# из-за округления на стороне плательщика, не должен требовать разбирательства.
SETTING_TOLERANCE = "billing.underpayment_tolerance_percent"

DEFAULT_DUE_DAYS = 7
# Проход биллинга раз в час: выставление привязано к календарю, а проверка
# оплаты — редкая точечная операция (ADR-0027), мгновенность ей не нужна.
PASS_INTERVAL_SECONDS = 60 * 60
# Перекрытие при опросе адреса счёта: провайдер индексирует переводы не мгно-
# венно, поэтому следующий опрос начинается чуть раньше прошлой отметки.
# Повторы гасит ключ идемпотентности платежа.
CHECK_OVERLAP = timedelta(hours=1)


def period_bounds(moment: datetime) -> tuple[datetime, datetime]:
	"""The calendar month (UTC) the moment falls into.

	Args:
		moment: Any moment inside the wanted period, naive UTC.

	Returns:
		``(start, end)`` — the first instant of the month and the first
		instant of the next one, so the period is ``[start, end)``.
	"""
	start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	end = start + timedelta(days=monthrange(start.year, start.month)[1])
	return start, end


def to_micro_usdt(amount: int, decimals: int) -> int:
	"""Value one asset amount in micro-USDT.

	First stage of ADR-0027: every asset the operator counts is a stablecoin
	valued one to one against USDT, so the conversion is a shift between
	decimal scales. An asset finer than USDT loses its tail — the remainder
	is dropped rather than rounded up, so the arithmetic never invents
	turnover the user did not have.

	Args:
		amount: Integer amount in the asset's minimal units.
		decimals: Decimal places of that asset.

	Returns:
		The amount in micro-USDT.
	"""
	if decimals == USDT_DECIMALS:
		return amount
	if decimals > USDT_DECIMALS:
		return amount // 10 ** (decimals - USDT_DECIMALS)
	return amount * 10 ** (USDT_DECIMALS - decimals)


def format_usdt(micro: int) -> str:
	"""An exact decimal string of a micro-USDT amount, trailing zeros trimmed."""
	return format_amount(micro, USDT_DECIMALS)


async def _turnover_contracts(registry: AsyncEngine, network: str) -> set[str]:
	"""Token contracts counted towards the turnover in one network.

	A broken setting degrades to "nothing counted here" with an error in the
	log — the same policy the watcher applies to its own list of contracts:
	a typo in the panel must never crash the billing pass.
	"""
	raw = await registry_ops.get_setting(registry, f"{SETTING_ASSETS_PREFIX}{network}")
	if not raw:
		return set()
	try:
		entries = json.loads(raw)
	except ValueError as exc:
		logger.error("invalid %s%s setting ignored: %s", SETTING_ASSETS_PREFIX, network, exc)
		return set()
	if not isinstance(entries, list):
		logger.error(
			"setting %s%s must be a JSON list of contract addresses; ignored",
			SETTING_ASSETS_PREFIX,
			network,
		)
		return set()
	contracts = set()
	for entry in entries:
		if not isinstance(entry, str) or not entry.strip():
			# Пустая строка — это нативная монета каталога; она стоит не
			# один к одному с USDT, а курсов у шлюза пока нет (ADR-0027).
			logger.error(
				"setting %s%s: only token contract addresses are accepted, %r ignored",
				SETTING_ASSETS_PREFIX,
				network,
				entry,
			)
			continue
		contracts.add(entry.strip())
	return contracts


async def turnover_assets(registry: AsyncEngine) -> dict[int, int]:
	"""Catalog assets counted towards the turnover: asset id → its decimals.

	An asset the operator listed but the gateway has never seen is simply
	absent from the catalog — and so are any transactions in it.
	"""
	counted: dict[int, int] = {}
	for network in sorted(chains.supported_networks()):
		contracts = await _turnover_contracts(registry, network)
		if not contracts:
			continue
		for asset in await registry_ops.list_assets(registry, network):
			if asset.contract_address in contracts:
				counted[asset.id] = asset.decimals
	return counted


async def user_turnover(
	engine: AsyncEngine, registry: AsyncEngine, *, since: datetime, until: datetime
) -> int:
	"""The user's turnover over a period, in micro-USDT (ADR-0027).

	Counts successful, finalized incoming operations in the assets the
	operator designated, leaving out moves between the user's own addresses.

	Args:
		engine: The user's own database engine.
		registry: Engine of the shared registry database.
		since: Period start, inclusive (naive UTC).
		until: Period end, exclusive (naive UTC).

	Returns:
		The turnover in micro-USDT; zero when the operator counts nothing.
	"""
	assets = await turnover_assets(registry)
	if not assets:
		return 0
	rows = await user_views.turnover_incoming(
		engine, since=since, until=until, asset_ids=set(assets)
	)
	return sum(to_micro_usdt(int(row.amount), assets[row.asset_id]) for row in rows)


@dataclass(frozen=True)
class Terms:
	"""The fee terms as they stood when an invoice was issued (ADR-0027).

	``rate_percent`` is kept exactly as the operator typed it — that string
	lands in the invoice, so a user reading it later sees the same number the
	panel showed. ``rate_hundredths`` is its integer form for the arithmetic:
	hundredths of a percent, so 1.5% is 150.
	"""

	rate_percent: str
	rate_hundredths: int
	threshold: int
	due_days: int


def _decimal_setting(raw: str | None, default: Decimal, key: str) -> Decimal:
	"""Read one decimal setting; a broken value degrades with an error in the log.

	Same policy as the watcher's numeric settings: a typo in the panel must
	never crash the billing pass.
	"""
	if not raw or not raw.strip():
		return default
	try:
		value = Decimal(raw.strip())
	except InvalidOperation:
		logger.error("invalid %s setting %r ignored, using %s", key, raw, default)
		return default
	if value < 0:
		logger.error("setting %s must not be negative (%s); using %s", key, value, default)
		return default
	return value


def _is_on(raw: str | None) -> bool:
	"""The gateway's convention for a switch stored as a setting string."""
	return (raw or "").strip().lower() in ("1", "true", "yes", "on")


async def read_terms(registry: AsyncEngine) -> Terms | None:
	"""The fee terms in force, or None when the fee is switched off.

	Returns:
		The terms, or None — either the switch is off or the rate is zero,
		which means the same thing in practice and saves a pass over users.
	"""
	if not _is_on(await registry_ops.get_setting(registry, SETTING_ENABLED)):
		return None
	rate = _decimal_setting(
		await registry_ops.get_setting(registry, SETTING_RATE), Decimal(0), SETTING_RATE
	)
	if rate == 0:
		return None
	threshold = _decimal_setting(
		await registry_ops.get_setting(registry, SETTING_THRESHOLD),
		Decimal(0),
		SETTING_THRESHOLD,
	)
	due_days = _decimal_setting(
		await registry_ops.get_setting(registry, SETTING_DUE_DAYS),
		Decimal(DEFAULT_DUE_DAYS),
		SETTING_DUE_DAYS,
	)
	return Terms(
		rate_percent=str(rate),
		# Сотые доли процента: ставка «x.xx%» ложится в целое без потерь,
		# и вся денежная арифметика остаётся целочисленной.
		rate_hundredths=int(rate * 100),
		threshold=int(threshold * MICRO_USDT),
		due_days=int(due_days) or DEFAULT_DUE_DAYS,
	)


def fee_amount(turnover: int, terms: Terms) -> int:
	"""The fee owed on a turnover, in micro-USDT.

	The rate applies to the whole turnover once the threshold is crossed, not
	to the excess above it (ADR-0027). The remainder of the division is
	dropped, so rounding never works against the user.
	"""
	if turnover <= terms.threshold:
		return 0
	return turnover * terms.rate_hundredths // (100 * 100)


def previous_period(moment: datetime) -> tuple[datetime, datetime]:
	"""The last period that has already ended before ``moment``."""
	start, _ = period_bounds(moment)
	return period_bounds(start - timedelta(days=1))


def _xpub_hash(xpub: str) -> str:
	"""Fingerprint of an extended public key — the same one the registry index uses."""
	return hashlib.sha256(xpub.encode()).hexdigest()


async def attach_master_wallet(
	billing: AsyncEngine, registry: AsyncEngine, *, network: str, xpub: str
) -> None:
	"""Set the owner's master wallet for one payment network.

	The key is validated by deriving address zero, exactly like a user's
	wallet, and checked against both halves of the "one xpub — one wallet"
	rule: the registry index of users' keys and the master wallets already
	entered (ADR-0027).

	Args:
		billing: The billing database engine.
		registry: Engine of the shared registry database.
		network: Payment network the wallet serves.
		xpub: Account-level extended public key of the owner.

	Raises:
		OperationError: unknown_network / invalid_xpub / private_key_rejected.
	"""
	family = chains.supported_networks().get(network)
	if family is None:
		raise OperationError("unknown_network", f"unknown network {network!r}")
	cleaned = xpub.strip()
	try:
		derive_address(family, cleaned, 0)
	except PrivateKeyError as exc:
		raise OperationError(
			"private_key_rejected",
			"an extended PRIVATE key was supplied; treat it as compromised "
			"and move the funds to a new wallet — the gateway needs the "
			"public account-level key (xpub) only",
		) from exc
	except InvalidKeyError as exc:
		raise OperationError("invalid_xpub", "the xpub was not accepted") from exc

	fingerprint = _xpub_hash(cleaned)
	# Половина правила «один xpub — один кошелёк», лежащая в реестре: ключ
	# владельца не должен совпасть с ключом пользователя, иначе один адрес
	# оказался бы у двух получателей и watcher не знал бы, чей это платёж.
	if await registry_ops.wallet_xpub_taken(registry, fingerprint):
		raise OperationError("invalid_xpub", "the xpub was not accepted")
	try:
		await billing_store.set_master_wallet(
			billing, network=network, xpub=cleaned, xpub_hash=fingerprint
		)
	except ValueError as exc:
		# Вторая половина: тот же ключ уже обслуживает другую сеть.
		raise OperationError("invalid_xpub", "the xpub was not accepted") from exc


# Замки выдачи индексов адресов счетов, по одному на сеть: чтение максимума и
# вставка обязаны быть атомарными, иначе два пользователя получат один адрес.
# Тот же приём, что у выдачи адресов приложениям; процесс один (ADR-0003), а
# уникальность (сеть, индекс) в схеме — страховка на случай иного.
_address_locks: dict[str, asyncio.Lock] = {}


def _address_lock(network: str) -> asyncio.Lock:
	"""The per-network lock serializing invoice-address allocation."""
	return _address_locks.setdefault(network, asyncio.Lock())


async def ensure_invoice_address(
	billing: AsyncEngine, *, user_id: int, network: str
) -> str | None:
	"""The user's permanent payment address in one network, deriving it if needed.

	Args:
		billing: The billing database engine.
		user_id: The user the address belongs to.
		network: Payment network of the address.

	Returns:
		The address, or None when the owner has no master wallet for that
		network — there is nowhere to be paid, and the caller reports it.
	"""
	existing = await billing_store.get_invoice_address(
		billing, user_id=user_id, network=network
	)
	if existing is not None:
		return existing.address
	wallet = await billing_store.get_master_wallet(billing, network)
	if wallet is None:
		return None
	family = chains.supported_networks().get(network)
	if family is None:
		logger.error("network %s has no implementation; invoice address not issued", network)
		return None

	async with _address_lock(network):
		# Перечитать под замком: адрес мог появиться, пока ждали очередь.
		existing = await billing_store.get_invoice_address(
			billing, user_id=user_id, network=network
		)
		if existing is not None:
			return existing.address
		index = await billing_store.next_invoice_index(billing, network)
		address = derive_address(family, wallet.xpub, index)
		try:
			await billing_store.insert_invoice_address(
				billing,
				user_id=user_id,
				network=network,
				address=address,
				derivation_index=index,
			)
		except IntegrityError as exc:
			existing = await billing_store.get_invoice_address(
				billing, user_id=user_id, network=network
			)
			if existing is None:
				raise RuntimeError(
					f"invoice address insert conflicted for user {user_id} "
					f"in {network} at index {index}: {exc.orig}"
				) from exc
			return existing.address
	return address


async def payment_network(billing: AsyncEngine, user_id: int) -> str:
	"""The user's payment network; the default one until they choose otherwise."""
	row = await billing_store.get_user_billing(billing, user_id)
	if row is None or not row.network:
		return DEFAULT_PAYMENT_NETWORK
	return row.network


@dataclass
class PassStats:
	"""Outcome of one billing pass."""

	# Пользователи, у которых оборот периода дал ненулевую комиссию, — то есть
	# те, кому счёт причитается. Раскладывается на три части: фактически
	# выставленные счета, случаи «выставлять некуда» и уже выставленные ранее
	# периоды (повторный проход их пропускает).
	users_billed: int = 0
	invoices_issued: int = 0
	invoices_overdue: int = 0
	# Пользователи, которым счёт посчитан, но выставить его некуда: у владельца
	# нет мастер-кошелька в их сети оплаты.
	no_master_wallet: list[str] = field(default_factory=list)


async def run_pass(data_dir: Path, *, now: datetime | None = None) -> PassStats:
	"""Run one billing pass: issue invoices for the finished period, mark overdue ones.

	Issuing is idempotent and not tied to the calendar day: the pass always
	bills the last period that has already ended, and the invoice key of the
	schema absorbs the repeats. A gateway that was down on the first of the
	month therefore issues its invoices at the next start, not never.

	Marking overdue invoices runs even when the fee is switched off: turning
	the fee off means "issue no new invoices", not "forgive the old ones".

	Args:
		data_dir: The gateway data directory.
		now: The moment the pass runs at; defaults to the current time
			(tests pass their own).

	Returns:
		Statistics of the pass.
	"""
	moment = now if now is not None else now_utc()
	stats = PassStats()
	journal = SecurityLog(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	billing = create_sqlite_engine(billing_db_path(data_dir))
	try:
		terms = await read_terms(registry)
		if terms is not None:
			await _issue_invoices(
				registry, billing, data_dir, moment=moment, terms=terms, stats=stats
			)
		else:
			logger.info("the gateway fee is switched off: no invoices are issued")
		overdue = await billing_store.mark_overdue(billing, now=moment)
		stats.invoices_overdue = len(overdue)
		for invoice_id, user_id in overdue:
			await _suspend(
				registry, billing, journal,
				user_id=user_id, invoice_id=invoice_id, moment=moment,
			)
	finally:
		journal.close()
		await billing.dispose()
		await registry.dispose()
	return stats


async def _suspend(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	*,
	user_id: int,
	invoice_id: int,
	moment: datetime,
) -> None:
	"""Close the gateway to a user whose invoice went past its deadline.

	The state is billing's own and is independent of the operator's block
	(ADR-0027): lifting one never lifts the other.
	"""
	if await billing_store.access_state(billing, user_id) == billing_store.ACCESS_SUSPENDED:
		return
	await billing_store.set_access_state(
		billing, user_id=user_id, state=billing_store.ACCESS_SUSPENDED, suspended_at=moment
	)
	logger.warning(
		"user %d suspended: invoice %d is past its due date", user_id, invoice_id
	)
	await _journal(registry, journal, "billing_suspend", user_id=user_id, invoice_id=invoice_id)


async def _restore(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	*,
	user_id: int,
	invoice_id: int,
) -> None:
	"""Let a user back in once nothing of theirs is awaiting money."""
	if await billing_store.access_state(billing, user_id) != billing_store.ACCESS_SUSPENDED:
		return
	if await billing_store.has_unpaid(billing, user_id):
		# Один счёт закрыт, но другой ещё ждёт денег — доступ не открываем.
		return
	await billing_store.set_access_state(
		billing, user_id=user_id, state=billing_store.ACCESS_OK, suspended_at=None
	)
	logger.info("user %d restored: nothing is awaiting payment any more", user_id)
	await _journal(registry, journal, "billing_restore", user_id=user_id, invoice_id=invoice_id)


async def _journal(
	registry: AsyncEngine,
	journal: SecurityLog,
	event: str,
	*,
	user_id: int,
	invoice_id: int,
) -> None:
	"""Record one access change in the security journal (ADR-0023).

	Приостановка и возврат доступа — события системы: их объявляет сам шлюз,
	без участия оператора и без действия пользователя.
	"""
	await journal.event(
		registry,
		event,
		actor=ACTOR_SYSTEM,
		outcome=OUTCOME_SUCCESS,
		user_id=user_id,
		detail={"invoice_id": invoice_id},
	)


async def _issue_invoices(
	registry: AsyncEngine,
	billing: AsyncEngine,
	data_dir: Path,
	*,
	moment: datetime,
	terms: Terms,
	stats: PassStats,
) -> None:
	"""Issue the finished period's invoices for every user that owes one."""
	period_start, period_end = previous_period(moment)
	due_at = moment + timedelta(days=terms.due_days)
	for user in await registry_ops.list_users(registry):
		db_path = user_db_path(data_dir, user.directory)
		if not db_path.exists():
			logger.warning("user %s has no database at %s, skipping", user.login, db_path)
			continue
		if await billing_store.invoice_exists(
			billing, user_id=user.id, period_start=period_start
		):
			continue
		engine = create_sqlite_engine(db_path)
		try:
			turnover = await user_turnover(
				engine, registry, since=period_start, until=period_end
			)
		except SQLAlchemyError:
			# Сбой базы одного владельца не срывает выставление остальным:
			# следующий проход попробует снова, ключ счёта не даст дубля.
			logger.exception("user %s: database unreadable, skipping this pass", user.login)
			continue
		finally:
			await engine.dispose()
		amount = fee_amount(turnover, terms)
		if amount <= 0:
			continue
		stats.users_billed += 1
		network = await payment_network(billing, user.id)
		address = await ensure_invoice_address(billing, user_id=user.id, network=network)
		if address is None:
			logger.error(
				"user %s owes %s USDT for %s but the gateway has no master wallet in %s;"
				" the invoice was not issued",
				user.login,
				format_usdt(amount),
				period_start.date(),
				network,
			)
			stats.no_master_wallet.append(user.login)
			continue
		invoice_id = await billing_store.insert_invoice(
			billing,
			user_id=user.id,
			period_start=period_start,
			period_end=period_end,
			turnover=turnover,
			rate_percent=terms.rate_percent,
			threshold=terms.threshold,
			amount=amount,
			due_at=due_at,
			network=network,
			address=address,
		)
		if invoice_id is None:
			continue  # период уже выставлен — повторный проход ничего не меняет
		stats.invoices_issued += 1
		logger.info(
			"invoice %d issued to %s for %s: %s USDT on turnover %s",
			invoice_id,
			user.login,
			period_start.date(),
			format_usdt(amount),
			format_usdt(turnover),
		)


async def run_forever(data_dir: Path) -> None:
	"""Run billing passes until cancelled.

	A failed pass is logged and does not stop the loop; cancellation
	propagates to the supervisor.
	"""
	while True:
		try:
			checked = await check_payments(data_dir)
			stats = await run_pass(data_dir)
			logger.info(
				"billing pass done: checked=%d payments=%d settled=%d foreign=%d"
				" billed=%d issued=%d overdue=%d no_wallet=%s",
				checked.addresses_checked,
				checked.payments_recorded,
				checked.invoices_settled,
				checked.foreign_assets,
				stats.users_billed,
				stats.invoices_issued,
				stats.invoices_overdue,
				",".join(stats.no_master_wallet) or "-",
			)
		except asyncio.CancelledError:
			raise
		except Exception:
			logger.exception("billing pass failed; continuing")
		await asyncio.sleep(PASS_INTERVAL_SECONDS)


async def _payment_assets(registry: AsyncEngine, network: str) -> set[str]:
	"""Token contracts accepted as payment in one network.

	A broken setting degrades to "nothing is accepted here" with an error in
	the log: a typo must not turn a stranger's token into a settled invoice.
	"""
	raw = await registry_ops.get_setting(
		registry, f"{SETTING_PAYMENT_ASSETS_PREFIX}{network}"
	)
	if not raw:
		return set()
	try:
		entries = json.loads(raw)
	except ValueError as exc:
		logger.error(
			"invalid %s%s setting ignored: %s", SETTING_PAYMENT_ASSETS_PREFIX, network, exc
		)
		return set()
	if not isinstance(entries, list):
		logger.error(
			"setting %s%s must be a JSON list of contract addresses; ignored",
			SETTING_PAYMENT_ASSETS_PREFIX,
			network,
		)
		return set()
	return {e.strip() for e in entries if isinstance(e, str) and e.strip()}


async def _tolerance(registry: AsyncEngine) -> Decimal:
	"""The underpayment tolerance, in percent of the invoice amount."""
	return _decimal_setting(
		await registry_ops.get_setting(registry, SETTING_TOLERANCE), Decimal(0), SETTING_TOLERANCE
	)


def _required(amount: int, tolerance: Decimal) -> int:
	"""How much must be credited for an invoice to count as settled."""
	if tolerance <= 0:
		return amount
	return int(amount - (amount * tolerance) // 100)


@dataclass
class CheckStats:
	"""Outcome of one payment check."""

	addresses_checked: int = 0
	payments_recorded: int = 0
	invoices_settled: int = 0
	# Поступления на адреса счетов в активах, которыми оплата не принимается.
	foreign_assets: int = 0


async def check_payments(
	data_dir: Path,
	*,
	now: datetime | None = None,
	source_factory=None,
) -> CheckStats:
	"""Poll the addresses of unpaid invoices and credit what arrived (ADR-0027).

	The billing check is deliberately independent of the watcher: it asks the
	provider for the confirmed transfers of a handful of addresses, and only
	of those whose invoices still await money. Nothing provisional is stored,
	so a chain reorganization has nothing to take back here — the provider
	reports finalized transfers only.

	Args:
		data_dir: The gateway data directory.
		now: The moment the check runs at; defaults to the current time.
		source_factory: Data source constructor; tests substitute a fake one.

	Returns:
		Statistics of the check.
	"""
	moment = now if now is not None else now_utc()
	factory = source_factory if source_factory is not None else chains.create_source
	stats = CheckStats()
	journal = SecurityLog(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	billing = create_sqlite_engine(billing_db_path(data_dir))
	try:
		unpaid = await billing_store.list_unpaid(billing)
		if not unpaid:
			return stats
		api_key = await registry_ops.get_setting(registry, chains.SETTING_API_KEY)
		rate = _decimal_setting(
			await registry_ops.get_setting(registry, chains.SETTING_RATE),
			Decimal(chains.DEFAULT_RATE_PER_SEC),
			chains.SETTING_RATE,
		)
		interval = float(1 / rate) if rate > 0 else 0.0
		tolerance = await _tolerance(registry)

		# По адресу может ждать несколько счетов: неоплаченный прошлый и
		# свежий. Опрашивается адрес один раз, зачёт идёт по порядку периодов.
		by_address: dict[tuple[str, str], list] = {}
		for invoice in unpaid:
			by_address.setdefault((invoice.network, invoice.address), []).append(invoice)

		for (network, address), invoices_here in sorted(by_address.items()):
			accepted = await _payment_assets(registry, network)
			try:
				source = factory(network, api_key, interval)
			except ValueError:
				logger.error("network %s has no data source; payments not checked", network)
				continue
			checked_at = min(
				(i.checked_at for i in invoices_here if i.checked_at is not None),
				default=None,
			)
			since = (checked_at - CHECK_OVERLAP) if checked_at is not None else None
			try:
				transfers = await source.transfers(address, since, True)
			except ChainDataSourceError as exc:
				# Провайдер недоступен или просит сбавить темп: курсор не
				# двигаем, следующий проход спросит то же самое.
				logger.warning("payment check for %s failed: %s", address, exc)
				continue
			finally:
				await source.aclose()
			stats.addresses_checked += 1

			for transfer in transfers:
				if transfer.direction != Direction.IN:
					continue
				if transfer.status != TransferStatus.SUCCESS:
					continue
				await _record_transfer(
					registry, billing, transfer, accepted=accepted, moment=moment, stats=stats
				)
			await billing_store.set_address_checked(
				billing, user_id=invoices_here[0].user_id, network=network, checked_at=moment
			)
			settled = await _credit_address(
				registry, billing, journal,
				address=address, invoices_here=invoices_here,
				tolerance=tolerance, moment=moment,
			)
			stats.invoices_settled += settled
	finally:
		journal.close()
		await billing.dispose()
		await registry.dispose()
	return stats


async def _record_transfer(
	registry: AsyncEngine,
	billing: AsyncEngine,
	transfer,
	*,
	accepted: set[str],
	moment: datetime,
	stats: CheckStats,
) -> None:
	"""Store one observed transfer on an invoice address."""
	asset = await registry_ops.get_or_create_asset(
		registry,
		network=transfer.asset.network,
		kind=KIND_NATIVE if transfer.asset.contract_address == "" else KIND_TOKEN,
		contract_address=transfer.asset.contract_address,
		symbol=transfer.asset.symbol,
		decimals=transfer.asset.decimals,
	)
	is_payment = transfer.asset.contract_address in accepted
	value = to_micro_usdt(transfer.amount, transfer.asset.decimals) if is_payment else 0
	recorded = await billing_store.record_payment(
		billing,
		network=transfer.network,
		address=transfer.address,
		txid=transfer.txid,
		asset_id=asset.id,
		amount=transfer.amount,
		value=value,
		tx_time=naive_utc(transfer.timestamp),
		finalized_at=moment,
	)
	if not recorded:
		return
	stats.payments_recorded += 1
	if is_payment:
		logger.info(
			"payment of %s USDT observed on invoice address %s (tx %s)",
			format_usdt(value),
			transfer.address,
			transfer.txid,
		)
	else:
		stats.foreign_assets += 1
		logger.warning(
			"foreign asset %s arrived on invoice address %s (tx %s); not credited",
			transfer.asset.symbol or transfer.asset.contract_address,
			transfer.address,
			transfer.txid,
		)


async def _credit_address(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	*,
	address: str,
	invoices_here: list,
	tolerance: Decimal,
	moment: datetime,
) -> int:
	"""Credit an address's uncredited money to its invoices, oldest first.

	Money follows the invoices in order, and whatever is left over stays
	uncredited on the payment — that leftover is the credit balance the next
	invoice will draw on (ADR-0027).

	Returns:
		How many invoices became settled.
	"""
	payments = await billing_store.creditable_payments(billing, address=address)
	# (id платежа, свободный остаток, уже зачтено) — расходуется по порядку.
	purse = [
		[row.id, int(row.value) - int(row.credited), int(row.credited)] for row in payments
	]
	settled = 0
	for invoice in sorted(invoices_here, key=lambda i: i.id):
		needed = _required(invoice.amount, tolerance) - invoice.credited
		if needed <= 0:
			continue
		allocations: list[tuple[int, int]] = []
		credited_total = invoice.credited
		for entry in purse:
			if needed <= 0:
				break
			if entry[1] <= 0:
				continue
			take = min(entry[1], needed)
			entry[1] -= take
			entry[2] += take
			credited_total += take
			needed -= take
			allocations.append((entry[0], entry[2]))
		if not allocations:
			continue
		paid = credited_total >= _required(invoice.amount, tolerance)
		await billing_store.apply_credits(
			billing,
			invoice_id=invoice.id,
			allocations=allocations,
			credited_total=credited_total,
			paid_at=moment if paid else None,
		)
		if paid:
			settled += 1
			logger.info(
				"invoice %d settled: %s USDT credited",
				invoice.id,
				format_usdt(credited_total),
			)
			# Доступ возвращается сразу по зачёту, без участия оператора
			# (ADR-0027) — и только если других долгов у пользователя нет.
			await _restore(
				registry, billing, journal,
				user_id=invoice.user_id, invoice_id=invoice.id,
			)
		else:
			logger.warning(
				"invoice %d underpaid: %s of %s USDT credited, access stays closed",
				invoice.id,
				format_usdt(credited_total),
				format_usdt(invoice.amount),
			)
	return settled


@dataclass(frozen=True)
class PanelInvoice:
	"""One invoice as the operator panel shows it."""

	id: int
	user_id: int
	username: str
	period: str
	turnover: str
	rate_percent: str
	amount: str
	credited: str
	due_at: str
	state: str
	network: str
	address: str
	paid_at: str | None
	manual_reason: str


def supported_payment_networks() -> set[str]:
	"""Networks an invoice can be issued in — those the gateway can watch at all."""
	return set(chains.supported_networks())


async def panel_invoices(
	billing: AsyncEngine, registry: AsyncEngine, *, state: str | None = None
) -> list[PanelInvoice]:
	"""Every user's invoices for the panel, newest period first.

	Args:
		billing: The billing database engine.
		registry: Engine of the shared registry database (for the logins).
		state: Filter by invoice state, or None for all of them.

	Raises:
		OperationError: invalid_state.
	"""
	if state is not None and state not in (
		billing_store.STATE_ISSUED,
		billing_store.STATE_PAID,
		billing_store.STATE_OVERDUE,
	):
		raise OperationError("invalid_state", f"unknown invoice state {state!r}")
	logins = {user.id: user.login for user in await registry_ops.list_users(registry)}
	rows = await billing_store.list_invoices(billing, state=state)
	return [
		PanelInvoice(
			id=row.id,
			user_id=row.user_id,
			# Пользователь мог быть удалён (ADR-0024), а счёт остаётся: долг
			# и его история переживают учётную запись.
			username=logins.get(row.user_id, "—"),
			period=row.period_start.date().isoformat(),
			turnover=format_usdt(int(row.turnover)),
			rate_percent=row.rate_percent,
			amount=format_usdt(int(row.amount)),
			credited=format_usdt(int(row.credited)),
			due_at=row.due_at.isoformat(sep=" ", timespec="minutes"),
			state=row.state,
			network=row.network,
			address=row.address,
			paid_at=row.paid_at.isoformat(sep=" ", timespec="minutes") if row.paid_at else None,
			manual_reason=row.manual_reason,
		)
		for row in rows
	]


async def confirm_manually(
	billing: AsyncEngine,
	registry: AsyncEngine,
	journal: SecurityLog,
	*,
	invoice_id: int,
	operator_id: int,
	reason: str,
	now: datetime | None = None,
) -> int:
	"""Settle an invoice on the operator's word and reopen the user's access.

	The path for money that reached the owner outside the gateway (ADR-0027).
	The result is exactly that of a credited payment, including the automatic
	restoration of access.

	Args:
		billing: The billing database engine.
		registry: Engine of the shared registry database.
		journal: The gateway's security journal.
		invoice_id: The invoice to settle.
		operator_id: The operator taking responsibility.
		reason: Why it is being settled by hand; stored with the invoice.
		now: Settlement time; defaults to the current moment.

	Returns:
		The id of the user whose invoice was settled.

	Raises:
		OperationError: unknown_invoice / invoice_already_paid / reason_required.
	"""
	if not reason.strip():
		# Причина обязательна: ручное закрытие долга — административное
		# действие над деньгами, и оно должно объяснять себя (ADR-0023).
		raise OperationError("reason_required", "state why the invoice is settled by hand")
	invoice = await billing_store.get_invoice(billing, invoice_id)
	if invoice is None:
		raise OperationError("unknown_invoice", f"invoice {invoice_id} does not exist")
	if invoice.state == billing_store.STATE_PAID:
		raise OperationError("invoice_already_paid", "this invoice is already settled")
	moment = now if now is not None else now_utc()
	await billing_store.mark_paid_manually(
		billing,
		invoice_id=invoice_id,
		operator_id=operator_id,
		reason=reason.strip(),
		paid_at=moment,
		amount=int(invoice.amount),
	)
	logger.info(
		"invoice %d settled manually by operator %d: %s", invoice_id, operator_id, reason.strip()
	)
	await _restore(registry, billing, journal, user_id=invoice.user_id, invoice_id=invoice_id)
	return invoice.user_id
