"""Billing operations: the fee the gateway owner takes from users (ADR-0027).

This module owns the turnover side of the fee: what a user earned over a
period and how that is valued. Issuing invoices, crediting payments and
suspending access build on it in their own modules.

The accounting unit is micro-USDT — an integer of the sixth decimal place,
the way USDT itself is denominated in TRON. Everything stays in integers:
a payment gateway may never compute money with floating point.
"""

from __future__ import annotations

import json
import logging
from calendar import monthrange
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_views

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
