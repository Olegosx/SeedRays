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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains, mail, settings_keys
from seedrays import settings_keys
from seedrays.chains.base import ChainDataSourceError, Direction, TransferStatus
from seedrays.mail.base import MailError
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
from seedrays.settings_keys import BILLING_ASSETS_PREFIX as SETTING_ASSETS_PREFIX

# Условия вознаграждения — настройки реестра (ADR-0016), страница панели.
# Выключено по умолчанию: установка не должна начать выставлять счета сама.
from seedrays.settings_keys import BILLING_DUE_DAYS as SETTING_DUE_DAYS
from seedrays.settings_keys import BILLING_ENABLED as SETTING_ENABLED
from seedrays.settings_keys import BILLING_RATE_PERCENT as SETTING_RATE
from seedrays.settings_keys import BILLING_THRESHOLD_USDT as SETTING_THRESHOLD

# Активы, которыми принимается оплата счетов, по сетям: JSON-список адресов
# контрактов. Всё остальное, пришедшее на адрес счёта, — чужой актив.
from seedrays.settings_keys import BILLING_PAYMENT_ASSETS_PREFIX as SETTING_PAYMENT_ASSETS_PREFIX
DEFAULT_DUE_DAYS = 7
# Ставка хранится в сотых долях процента: «x.xx%» ложится в целое без потерь,
# и вся денежная арифметика остаётся целочисленной. Точнее задать ставку
# нельзя — панель отвергает такое значение при сохранении.
RATE_HUNDREDTHS_IN_PERCENT = 100
RATE_PLACES = 2
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
	# Decimal("nan") и Decimal("inf") разбираются без ошибки, а дальше
	# роняют арифметику прохода: для политики «битое значение деградирует
	# к умолчанию» они такие же битые, как буквы.
	if not value.is_finite():
		logger.error("invalid %s setting %r ignored, using %s", key, raw, default)
		return default
	if value < 0:
		logger.error("setting %s must not be negative (%s); using %s", key, value, default)
		return default
	return value


def _percent_text(hundredths: int) -> str:
	"""The rate as text, from its hundredths of a percent: 150 → "1.5"."""
	return str((Decimal(hundredths) / RATE_HUNDREDTHS_IN_PERCENT).normalize())


async def read_terms(registry: AsyncEngine) -> Terms | None:
	"""The fee terms in force, or None when the fee is switched off.

	Returns:
		The terms, or None — either the switch is off or the rate is zero,
		which means the same thing in practice and saves a pass over users.
	"""
	if not settings_keys.is_on(await registry_ops.get_setting(registry, SETTING_ENABLED)):
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
	hundredths = int(rate * RATE_HUNDREDTHS_IN_PERCENT)
	if hundredths != rate * RATE_HUNDREDTHS_IN_PERCENT:
		# Значение точнее двух знаков панель не принимает; попасть в базу
		# оно может только правкой руками. Считаем по усечённой ставке и
		# её же пишем в счёт: документ, противоречащий сам себе, хуже
		# документа с чуть меньшей ставкой.
		logger.warning(
			"the fee rate %s is finer than %d decimal places; applying %s%% instead",
			rate,
			RATE_PLACES,
			_percent_text(hundredths),
		)
	if hundredths == 0:
		# Иначе проход отработал бы молча и не выставил ни одного счёта:
		# в журнале «выставлено 0» не отличить от «никто не превысил порог».
		logger.error(
			"the fee rate %s rounds down to zero at %d decimal places: "
			"no invoice can be issued until it is corrected",
			rate,
			RATE_PLACES,
		)
		return None
	return Terms(
		rate_percent=_percent_text(hundredths),
		rate_hundredths=hundredths,
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
	return turnover * terms.rate_hundredths // (100 * RATE_HUNDREDTHS_IN_PERCENT)


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


# Вычисляемые состояния счёта. В базе их нет: оплаченность выводится из
# баланса пользователя (решение владельца) — деньги покрывают счета в
# порядке выставления, распределение нигде не хранится.
STATE_ISSUED = "issued"
STATE_PAID = "paid"
STATE_OVERDUE = "overdue"


def classify_invoices(rows: list, credits: int, now: datetime) -> list[tuple]:
	"""Assign each invoice its computed state, oldest first.

	Правило владельца: баланс — сумма зачислений минус сумма счетов; ноль и
	выше — всё в порядке. По счетам это разворачивается так: счёт оплачен,
	когда зачислений хватает на него и на все счета старше него; непокрытый
	счёт с прошедшим сроком — просрочен, иначе — ожидает оплаты.

	Args:
		rows: Invoice rows sorted oldest first (as ``list_invoices`` returns).
		credits: The user's total credits in micro-USDT.
		now: The moment the states are computed at.

	Returns:
		``(row, state)`` pairs in the same order.
	"""
	classified = []
	cumulative = 0
	for row in rows:
		cumulative += int(row.amount)
		if credits >= cumulative:
			state = STATE_PAID
		elif row.due_at < now:
			state = STATE_OVERDUE
		else:
			state = STATE_ISSUED
		classified.append((row, state))
	return classified


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
	"""Run one billing pass: issue the finished period's invoices, reconcile access.

	Issuing is idempotent and not tied to the calendar day: the pass always
	bills the last period that has already ended, and the invoice key of the
	schema absorbs the repeats. A gateway that was down on the first of the
	month therefore issues its invoices at the next start, not never.

	The access reconciliation runs even when the fee is switched off: turning
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
		notifier = await Notifier.build(registry)
		terms = await read_terms(registry)
		if terms is not None:
			await _issue_invoices(
				registry, billing, data_dir,
				moment=moment, terms=terms, stats=stats, notifier=notifier,
			)
		else:
			logger.info("the gateway fee is switched off: no invoices are issued")
		stats.invoices_overdue = await _reconcile_access(
			registry, billing, journal, moment=moment, notifier=notifier
		)
	finally:
		journal.close()
		await billing.dispose()
		await registry.dispose()
	return stats


async def _reconcile_access(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	*,
	moment: datetime,
	notifier: "Notifier",
) -> int:
	"""Bring every user's access state in line with what their balance says.

	Сверка, а не реакция на событие. Прежде доступ закрывался только тем,
	чьи счета перевёл в просрочку именно этот вызов, — и сбой на шаге
	приостановки оставлял просроченный счёт при открытом доступе навсегда:
	следующий проход этих пользователей уже не видел, потому что счёт был
	помечен. Теперь состояние выводится из того, что сейчас в базе, поэтому
	любой сбой лечится следующим проходом.

	Обе операции смены состояния идемпотентны — молчат, когда менять нечего,
	— так что повторные проходы не засоряют ни журнал безопасности, ни лог.
	Сбой на одном пользователе не уносит остальных (ADR-0007): его состояние
	приведёт следующая сверка.

	Args:
		registry: The registry engine (the journal writes user logins).
		billing: The billing database engine.
		journal: The security journal.
		moment: The moment the pass runs at.
	"""
	remind_days = int(
		_decimal_setting(
			await registry_ops.get_setting(registry, settings_keys.BILLING_REMIND_DAYS),
			Decimal(0),
			settings_keys.BILLING_REMIND_DAYS,
		)
	)
	balances = await billing_store.user_balances(billing)
	overdue_total = 0
	owing: set[int] = set()
	for user_id, balance in balances.items():
		if balance >= 0:
			continue
		# Должник. Просрочен ли долг — отвечает классификация его счетов.
		rows = await billing_store.list_invoices(billing, user_id=user_id)
		credits = await billing_store.user_credits(billing, user_id)
		classified = classify_invoices(rows, credits, moment)
		if remind_days > 0:
			await _remind(
				registry, billing, notifier,
				user_id=user_id, balance=balance, classified=classified,
				moment=moment, remind_days=remind_days,
			)
		overdue_ids = [row.id for row, state in classified if state == STATE_OVERDUE]
		overdue_total += len(overdue_ids)
		if not overdue_ids:
			continue
		owing.add(user_id)
		try:
			await _suspend(
				registry, billing, journal, notifier,
				user_id=user_id, invoice_id=overdue_ids[0], moment=moment,
			)
		except SQLAlchemyError:
			logger.exception("user %d: suspending for non-payment failed", user_id)
	for user_id in await billing_store.users_in_access_state(
		billing, billing_store.ACCESS_SUSPENDED
	):
		if user_id in owing:
			continue
		try:
			await _restore(registry, billing, journal, notifier, user_id=user_id)
		except SQLAlchemyError:
			logger.exception("user %d: restoring access failed", user_id)
	return overdue_total


async def _remind(
	registry: AsyncEngine,
	billing: AsyncEngine,
	notifier: "Notifier",
	*,
	user_id: int,
	balance: int,
	classified: list[tuple],
	moment: datetime,
	remind_days: int,
) -> None:
	"""Remind about an unpaid invoice approaching its due date; once per invoice."""
	for row, state in classified:
		if state != STATE_ISSUED:
			continue
		if row.due_at - moment > timedelta(days=remind_days):
			continue
		if not await billing_store.mark_notified(
			billing, kind="invoice_reminder", subject=f"invoice:{row.id}"
		):
			continue
		await notifier.invoice_reminder(
			registry,
			user_id=user_id,
			debt=-balance,
			due_at=row.due_at,
			network=row.network,
			address=row.address,
		)


async def _suspend(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	notifier: "Notifier",
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
	await notifier.access_suspended(registry, user_id=user_id)


async def _restore(
	registry: AsyncEngine,
	billing: AsyncEngine,
	journal: SecurityLog,
	notifier: "Notifier",
	*,
	user_id: int,
	invoice_id: int | None = None,
) -> None:
	"""Let a user back in: the reconciliation found no overdue debt on them."""
	if await billing_store.access_state(billing, user_id) != billing_store.ACCESS_SUSPENDED:
		return
	await billing_store.set_access_state(
		billing, user_id=user_id, state=billing_store.ACCESS_OK, suspended_at=None
	)
	logger.info("user %d restored: no overdue debt any more", user_id)
	await _journal(registry, journal, "billing_restore", user_id=user_id, invoice_id=invoice_id)
	await notifier.access_restored(registry, user_id=user_id)


async def _journal(
	registry: AsyncEngine,
	journal: SecurityLog,
	event: str,
	*,
	user_id: int,
	invoice_id: int | None,
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
	notifier: "Notifier",
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
			# Письмо владельцу — один раз на пользователя и период, а не
			# каждым проходом заново.
			if await billing_store.mark_notified(
				billing,
				kind="no_master_wallet",
				subject=f"user:{user.id}:{period_start.date().isoformat()}",
			):
				await notifier.owner_no_wallet(
					login=user.login, network=network, amount=amount
				)
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
		# Выставление случается один раз (ключ счёта) — письмо о нём тоже.
		await notifier.invoice_issued(
			registry,
			user_id=user.id,
			amount=amount,
			due_at=due_at,
			network=network,
			address=address,
		)
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
			# Сначала выставление: свежий счёт делает пользователя должником,
			# и его адрес попадает в опрос и сверку этого же прохода.
			stats = await run_pass(data_dir)
			checked = await check_payments(data_dir)
			logger.info(
				"billing pass done: checked=%d payments=%d foreign=%d"
				" billed=%d issued=%d overdue=%d no_wallet=%s",
				checked.addresses_checked,
				checked.payments_recorded,
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


@dataclass
class CheckStats:
	"""Outcome of one payment check."""

	addresses_checked: int = 0
	payments_recorded: int = 0
	# Поступления на адреса счетов в активах, которыми оплата не принимается.
	foreign_assets: int = 0


async def check_payments(
	data_dir: Path,
	*,
	now: datetime | None = None,
	source_factory=None,
) -> CheckStats:
	"""Poll the debtors' invoice addresses and credit what arrived (ADR-0027).

	The billing check is deliberately independent of the watcher: it asks the
	provider for the confirmed transfers of a handful of addresses, and only
	of those whose owner's balance is below zero. Nothing provisional is
	stored, so a chain reorganization has nothing to take back here — the
	provider reports finalized transfers only.

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
		notifier = await Notifier.build(registry)
		balances = await billing_store.user_balances(billing)
		debtors = {user_id for user_id, balance in balances.items() if balance < 0}
		targets = await billing_store.poll_addresses(billing, debtors)
		if not targets:
			return stats
		api_key = await registry_ops.get_setting(registry, chains.SETTING_API_KEY)
		rate = _decimal_setting(
			await registry_ops.get_setting(registry, chains.SETTING_RATE),
			Decimal(chains.DEFAULT_RATE_PER_SEC),
			chains.SETTING_RATE,
		)
		interval = float(1 / rate) if rate > 0 else 0.0

		# Первая фаза: опросить провайдера и записать наблюдения. Сбой по
		# адресу законно откладывает его до следующего прохода.
		for target in targets:
			accepted = await _payment_assets(registry, target.network)
			try:
				source = factory(target.network, api_key, interval)
			except ValueError:
				logger.error(
					"network %s has no data source; payments not checked", target.network
				)
				continue
			since = (
				(target.checked_at - CHECK_OVERLAP)
				if target.checked_at is not None
				else None
			)
			try:
				transfers = await source.transfers(target.address, since, True)
			except ChainDataSourceError as exc:
				# Провайдер недоступен или просит сбавить темп: курсор не
				# двигаем, следующий проход спросит то же самое.
				logger.warning("payment check for %s failed: %s", target.address, exc)
				continue
			finally:
				await source.aclose()
			stats.addresses_checked += 1

			for transfer in _merged_by_transaction(transfers):
				await _record_transfer(
					registry, billing, notifier, transfer,
					user_id=target.user_id, accepted=accepted, moment=moment, stats=stats,
				)
			await billing_store.set_address_checked(
				billing, user_id=target.user_id, network=target.network, checked_at=moment
			)

		# Вторая фаза — сверка: состояние доступа выводится из балансов,
		# какие уже лежат в базе. От исхода опроса она не зависит — иначе
		# деньги, записанные прошлым проходом, не вернули бы доступ, пока
		# провайдер недоступен.
		await _reconcile_access(registry, billing, journal, moment=moment, notifier=notifier)
	finally:
		journal.close()
		await billing.dispose()
		await registry.dispose()
	return stats


def _merged_by_transaction(transfers: list) -> list:
	"""Successful incoming transfers, merged per transaction, address and asset.

	Одна оплата может прийти несколькими переводами внутри одной
	транзакции. Различить их в ключе идемпотентности нечем: адресный
	эндпоинт провайдера порядкового номера события не сообщает — в
	отличие от событийного, которым пользуется watcher. Поэтому переводы
	одного актива одной транзакции на один адрес складываются в одну
	запись: ключ остаётся устойчивым к повторному опросу, а сумма верна.
	Иначе второй перевод отбрасывался бы как дубль первого, и полностью
	оплаченный счёт оставался бы недоплаченным, а доступ — закрытым.

	Args:
		transfers: Transfers as the provider returned them.

	Returns:
		One transfer per (transaction, address, asset), carrying the sum.
	"""
	merged: dict[tuple[str, str, str], object] = {}
	for transfer in transfers:
		if transfer.direction != Direction.IN:
			continue
		if transfer.status != TransferStatus.SUCCESS:
			continue
		key = (transfer.txid, transfer.address, transfer.asset.contract_address)
		known = merged.get(key)
		merged[key] = (
			transfer
			if known is None
			else replace(known, amount=known.amount + transfer.amount)
		)
	return list(merged.values())


async def _record_transfer(
	registry: AsyncEngine,
	billing: AsyncEngine,
	notifier: "Notifier",
	transfer,
	*,
	user_id: int,
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
	if asset.decimals != transfer.asset.decimals:
		# Разрядность берётся из каталога, а не из ответа: ею умножается
		# сумма, и подменённое значение переоценило бы и этот платёж, и
		# любой прошлый. Расхождение — повод посмотреть, а не повод
		# пересчитать деньги по слову провайдера.
		logger.warning(
			"asset %s reports %d decimals, the catalog holds %d; valuing by the catalog",
			transfer.asset.contract_address or transfer.asset.symbol,
			transfer.asset.decimals,
			asset.decimals,
		)
	is_payment = transfer.asset.contract_address in accepted
	value = to_micro_usdt(transfer.amount, asset.decimals) if is_payment else 0
	recorded = await billing_store.record_payment(
		billing,
		network=transfer.network,
		address=transfer.address,
		txid=transfer.txid,
		asset_id=asset.id,
		user_id=user_id,
		amount=transfer.amount,
		value=value,
		tx_time=naive_utc(transfer.timestamp),
		finalized_at=moment,
	)
	if recorded == billing_store.PAYMENT_KNOWN:
		return
	if recorded == billing_store.PAYMENT_EXTENDED:
		# Прошлый ответ провайдера был неполным: в транзакции оказалось
		# больше переводов на этот адрес, чем он показал тогда.
		logger.warning(
			"payment on invoice address %s (tx %s) topped up to %s USDT: "
			"the earlier answer held less of the same transaction",
			transfer.address,
			transfer.txid,
			format_usdt(value),
		)
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
		# Запись только что состоялась (PAYMENT_NEW) — идемпотентность письма
		# даром: повторный опрос тот же перевод заново не запишет.
		await notifier.owner_foreign_asset(
			network=transfer.network,
			address=transfer.address,
			txid=transfer.txid,
			symbol=transfer.asset.symbol or transfer.asset.contract_address,
		)


def supported_payment_networks() -> set[str]:
	"""Networks an invoice can be issued in — those the gateway can watch at all."""
	return set(chains.supported_networks())


@dataclass(frozen=True)
class PanelInvoice:
	"""One invoice as the operator panel shows it, with its computed state."""

	id: int
	user_id: int
	username: str
	period: str
	turnover: str
	rate_percent: str
	amount: str
	due_at: str
	state: str
	network: str
	address: str


async def panel_invoices(
	billing: AsyncEngine,
	registry: AsyncEngine,
	*,
	state: str | None = None,
	now: datetime | None = None,
) -> tuple[list[PanelInvoice], dict[int, str]]:
	"""Every user's invoices with computed states, newest period first.

	Args:
		billing: The billing database engine.
		registry: Engine of the shared registry database (for the logins).
		state: Filter by computed invoice state, or None for all of them.
		now: The moment states are computed at; defaults to the current time.

	Returns:
		``(invoices, balances)`` — the rows for the panel and each involved
		user's balance as an exact decimal string.

	Raises:
		OperationError: invalid_state.
	"""
	if state is not None and state not in (STATE_ISSUED, STATE_PAID, STATE_OVERDUE):
		raise OperationError("invalid_state", f"unknown invoice state {state!r}")
	moment = now if now is not None else now_utc()
	logins = {user.id: user.login for user in await registry_ops.list_users(registry)}
	balances = await billing_store.user_balances(billing)
	rows = await billing_store.list_invoices(billing)
	by_user: dict[int, list] = {}
	for row in rows:
		by_user.setdefault(row.user_id, []).append(row)
	result = []
	for user_id, user_rows in by_user.items():
		credits = await billing_store.user_credits(billing, user_id)
		for row, computed in classify_invoices(user_rows, credits, moment):
			if state is not None and computed != state:
				continue
			result.append(
				PanelInvoice(
					id=row.id,
					user_id=row.user_id,
					# Пользователь мог быть удалён (ADR-0024), а счёт
					# остаётся: долг переживает учётную запись.
					username=logins.get(row.user_id, "—"),
					period=row.period_start.date().isoformat(),
					turnover=format_usdt(int(row.turnover)),
					rate_percent=row.rate_percent,
					amount=format_usdt(int(row.amount)),
					due_at=row.due_at.isoformat(sep=" ", timespec="minutes"),
					state=computed,
					network=row.network,
					address=row.address,
				)
			)
	result.sort(key=lambda invoice: (invoice.period, invoice.id), reverse=True)
	return result, {
		user_id: format_usdt(balance) for user_id, balance in balances.items()
	}


async def confirm_manually(
	billing: AsyncEngine,
	registry: AsyncEngine,
	journal: SecurityLog,
	*,
	invoice_id: int,
	operator_id: int,
	reason: str,
	amount: str | None = None,
	now: datetime | None = None,
) -> int:
	"""Credit money that arrived outside the gateway, on the operator's word.

	В балансовой модели это ручное ЗАЧИСЛЕНИЕ, а не пометка счёта: сумма
	падает на баланс пользователя, оплаченность счетов из него выводится, и
	сверка тут же возвращает доступ, если долга больше нет.

	Args:
		billing: The billing database engine.
		registry: Engine of the shared registry database.
		journal: The gateway's security journal.
		invoice_id: The invoice the operator is looking at; names the user
			and the default amount.
		operator_id: The operator taking responsibility.
		reason: Why the money is credited by hand; stored with the credit.
		amount: The amount in USDT as a decimal string; None credits the
			invoice's exact amount.
		now: The reconciliation moment; defaults to the current time.

	Returns:
		The id of the user credited.

	Raises:
		OperationError: unknown_invoice / reason_required / invalid_amount.
	"""
	if not reason.strip():
		# Причина обязательна: ручное зачисление — административное
		# действие над деньгами, и оно должно объяснять себя (ADR-0023).
		raise OperationError("reason_required", "state why the money is credited by hand")
	invoice = await billing_store.get_invoice(billing, invoice_id)
	if invoice is None:
		raise OperationError("unknown_invoice", f"invoice {invoice_id} does not exist")
	if amount is None or not amount.strip():
		value = int(invoice.amount)
	else:
		try:
			value = int(Decimal(amount.strip()) * MICRO_USDT)
		except InvalidOperation:
			raise OperationError(
				"invalid_amount", "the amount must be a decimal number of USDT"
			) from None
		if value <= 0:
			raise OperationError("invalid_amount", "the amount must be positive")
	moment = now if now is not None else now_utc()
	await billing_store.add_manual_credit(
		billing,
		user_id=invoice.user_id,
		value=value,
		operator_id=operator_id,
		reason=reason.strip(),
	)
	logger.info(
		"operator %d credited %s USDT to user %d by hand: %s",
		operator_id,
		format_usdt(value),
		invoice.user_id,
		reason.strip(),
	)
	await _reconcile_access(
		registry, billing, journal, moment=moment, notifier=await Notifier.build(registry)
	)
	return invoice.user_id


async def cabinet_view(
	billing: AsyncEngine, user_id: int, *, now: datetime | None = None
) -> dict:
	"""The cabinet's billing section: the balance, the invoices, where to pay.

	Answers the user's two questions — how much and where. The balance is the
	single figure of merit (ADR-0027): zero or above means everything is in
	order, below zero is the debt to be paid to the shown address.

	Args:
		billing: The billing database engine.
		user_id: The signed-in user.
		now: The moment states are computed at; defaults to the current time.
	"""
	moment = now if now is not None else now_utc()
	credits = await billing_store.user_credits(billing, user_id)
	rows = await billing_store.list_invoices(billing, user_id=user_id)
	owed = sum(int(row.amount) for row in rows)
	balance = credits - owed
	network = await payment_network(billing, user_id)
	address_row = await billing_store.get_invoice_address(
		billing, user_id=user_id, network=network
	)
	invoices = [
		{
			"period": row.period_start.date().isoformat(),
			"turnover": format_usdt(int(row.turnover)),
			"rate_percent": row.rate_percent,
			"amount": format_usdt(int(row.amount)),
			"due_at": row.due_at.isoformat(sep=" ", timespec="minutes"),
			"state": state,
			"network": row.network,
			"address": row.address,
		}
		for row, state in reversed(classify_invoices(rows, credits, moment))
	]
	wallets = await billing_store.list_master_wallets(billing)
	return {
		"balance": format_usdt(balance),
		"debt": format_usdt(-balance) if balance < 0 else "0",
		"network": network,
		# Куда платить: постоянный адрес пользователя в его сети оплаты.
		# Появляется вместе с первым счётом — раньше платить не за что.
		"address": address_row.address if address_row is not None else None,
		"invoices": invoices,
		# Сети, доступные для выбора: те, где владелец завёл мастер-кошелёк.
		"networks": sorted(wallet.network for wallet in wallets),
	}


async def choose_payment_network(
	billing: AsyncEngine, *, user_id: int, network: str
) -> None:
	"""Store the user's payment network choice.

	Raises:
		OperationError: unknown_network — сети нет среди мастер-кошельков;
			debt_pending — реквизиты выставленного счёта менять нельзя, пока
			долг не погашен (ADR-0027).
	"""
	wallet = await billing_store.get_master_wallet(billing, network)
	if wallet is None:
		raise OperationError(
			"unknown_network", f"network {network!r} is not available for payment"
		)
	credits = await billing_store.user_credits(billing, user_id)
	rows = await billing_store.list_invoices(billing, user_id=user_id)
	owed = sum(int(row.amount) for row in rows)
	if credits - owed < 0:
		raise OperationError(
			"debt_pending",
			"settle the outstanding invoice first: its payment details must not move",
		)
	await billing_store.set_payment_network(billing, user_id=user_id, network=network)


class Notifier:
	"""Sends the billing emails of one pass (ADR-0027, ADR-0020).

	Собирается один раз на проход: почтовик и адреса решаются в момент
	создания. Без настроенной почты каждое письмо превращается в строку
	журнала — проход от почты не зависит никогда.
	"""

	def __init__(self, mailer, owner_email: str, base_url: str) -> None:
		self._mailer = mailer
		self._owner = owner_email.strip()
		self._base = base_url.rstrip("/")

	@classmethod
	async def build(cls, registry: AsyncEngine) -> "Notifier":
		"""Resolve the mailer and the owner's address from the settings."""
		mailer = await mail.from_settings(registry)
		owner = await registry_ops.get_setting(registry, settings_keys.BILLING_OWNER_EMAIL)
		base = await registry_ops.get_setting(registry, settings_keys.GATEWAY_BASE_URL)
		return cls(mailer, owner or "", base or "")

	def _billing_link(self) -> str:
		"""The cabinet's invoices page, when the gateway knows its address."""
		return f"\n\nYour balance and payment details: {self._base}/billing.html" if self._base else ""

	async def _send(self, to: str, subject: str, text: str) -> None:
		if self._mailer is None or not to:
			logger.info("billing mail skipped (mail not set up): %s", subject)
			return
		try:
			await self._mailer.send(to, subject, text)
		except MailError as exc:
			# Письмо вторично: сбой почты не должен трогать ни выставление,
			# ни зачёт, ни доступ (ADR-0020).
			logger.error("billing mail %r to %s failed: %s", subject, to, exc)

	async def _user_email(self, registry: AsyncEngine, user_id: int) -> str:
		"""The user's confirmed primary address, or nothing to send to."""
		emails = await registry_ops.list_user_emails(registry, user_id)
		primary = next((e for e in emails if e.is_primary), None)
		if primary is None or primary.confirmed_at is None:
			return ""
		return primary.address

	async def invoice_issued(
		self, registry: AsyncEngine, *, user_id: int, amount: int, due_at: datetime,
		network: str, address: str,
	) -> None:
		"""Tell the user a new invoice exists and how to pay it."""
		await self._send(
			await self._user_email(registry, user_id),
			"SeedRays: a gateway fee invoice was issued",
			f"The gateway issued an invoice of {format_usdt(amount)} USDT for the past "
			f"month.\nPay it before {due_at.date().isoformat()} with a stablecoin "
			f"transfer to your payment address in {network}:\n{address}"
			+ self._billing_link(),
		)

	async def invoice_reminder(
		self, registry: AsyncEngine, *, user_id: int, debt: int, due_at: datetime,
		network: str, address: str,
	) -> None:
		"""Remind the user the due date is close and the balance is short."""
		await self._send(
			await self._user_email(registry, user_id),
			"SeedRays: the gateway fee is due soon",
			f"Your balance is short {format_usdt(debt)} USDT and the due date is "
			f"{due_at.date().isoformat()}.\nTop up your payment address in {network}:\n"
			f"{address}\n\nAfter the due date the gateway access is suspended until "
			"the balance is settled." + self._billing_link(),
		)

	async def access_suspended(self, registry: AsyncEngine, *, user_id: int) -> None:
		"""Tell the user their access closed over an overdue debt."""
		await self._send(
			await self._user_email(registry, user_id),
			"SeedRays: gateway access suspended over an unpaid fee",
			"The gateway fee invoice is past its due date, so access to the gateway "
			"and its API is suspended.\nSettle the balance and access returns "
			"automatically." + self._billing_link(),
		)

	async def access_restored(self, registry: AsyncEngine, *, user_id: int) -> None:
		"""Tell the user their access is back."""
		await self._send(
			await self._user_email(registry, user_id),
			"SeedRays: gateway access restored",
			"The gateway fee is settled and access to the gateway and its API is "
			"restored. Thank you." + self._billing_link(),
		)

	async def owner_foreign_asset(
		self, *, network: str, address: str, txid: str, symbol: str
	) -> None:
		"""Tell the owner a stray asset landed on an invoice address."""
		await self._send(
			self._owner,
			"SeedRays: a foreign asset arrived on an invoice address",
			f"A transfer of {symbol!r} arrived on the invoice address {address} "
			f"({network}, tx {txid}).\nIt is not an accepted payment asset and was "
			"not credited.",
		)

	async def owner_no_wallet(self, *, login: str, network: str, amount: int) -> None:
		"""Tell the owner an invoice could not be issued: no master wallet."""
		await self._send(
			self._owner,
			"SeedRays: an invoice could not be issued",
			f"User {login!r} owes {format_usdt(amount)} USDT for the past month, but "
			f"the gateway has no master wallet in {network!r} — there is nowhere to "
			"issue the invoice to.\nEnter the master wallet on the panel's invoices "
			"page, and the next pass will issue it.",
		)
