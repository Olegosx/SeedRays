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
from seedrays.derivation.derive import InvalidKeyError, PrivateKeyError, derive_address
from seedrays.orchestrator.operations import OperationError
from seedrays.storage import billing as billing_store
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_views
from seedrays.storage.engine import (
	billing_db_path,
	create_sqlite_engine,
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

DEFAULT_DUE_DAYS = 7
# Проход биллинга раз в сутки: выставление привязано к календарю, а не к
# частоте опроса, поэтому чаще смысла нет.
PASS_INTERVAL_SECONDS = 24 * 60 * 60


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
	sign = "-" if micro < 0 else ""
	whole, fraction = divmod(abs(micro), MICRO_USDT)
	tail = str(fraction).rjust(USDT_DECIMALS, "0").rstrip("0")
	return f"{sign}{whole}.{tail}" if tail else f"{sign}{whole}"


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
	registry = create_sqlite_engine(registry_db_path(data_dir))
	billing = create_sqlite_engine(billing_db_path(data_dir))
	try:
		terms = await read_terms(registry)
		if terms is not None:
			await _issue_invoices(
				registry, billing, data_dir, moment=moment, terms=terms, stats=stats
			)
		else:
			logger.info("billing pass: the fee is switched off, issuing nothing")
		stats.invoices_overdue = len(await billing_store.mark_overdue(billing, now=moment))
	finally:
		await billing.dispose()
		await registry.dispose()
	return stats


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
			stats = await run_pass(data_dir)
			logger.info(
				"billing pass done: billed=%d issued=%d overdue=%d no_wallet=%s",
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
