"""Orchestrator operations: the business core behind the API layers (ADR-0011).

The HTTP layer stays a thin adapter: every rule about bindings, balances
and history lives here. Database access goes through the storage layer
(ADR-0006) — this module never sees SQL.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.derivation.derive import derive_address
from seedrays.families import Family
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_apps, user_store, user_views, user_wallets
from seedrays.storage.engine import create_sqlite_engine, user_db_path
from seedrays.storage.user_store import API_STATUS_ALL, HISTORY_STATUS_FILTERS, classify_transaction

DEFAULT_PAGE_LIMIT = 10


class OperationError(Exception):
	"""A business-rule violation with a machine code (unified error format)."""

	def __init__(self, code: str, message: str) -> None:
		super().__init__(message)
		self.code = code
		self.message = message


def hash_api_key(api_key: str) -> str:
	"""Hash an application API key for storage and lookup.

	API keys are high-entropy random tokens, so a fast hash is the right
	tool (ADR: slow password hashing is for human passwords only).
	"""
	return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass
class AppContext:
	"""The resolved caller of the Application API."""

	user_id: int
	application_id: int
	application_name: str
	# Экземпляр приложения (ADR-0025): пространство имён идентификаторов
	# пользователей. Свойство вызывающего, а не отдельной операции, поэтому
	# живёт здесь — операции ниже его сигнатур не меняют.
	instance: str
	engine: AsyncEngine


async def resolve_application(
	registry: AsyncEngine, data_dir: Path, api_key: str, instance: str = ""
) -> AppContext | None:
	"""Resolve an API key to its application, or None if the key is unknown.

	Lookup: the registry key index gives the owning user, the user's own
	database gives the application (ADR-0008, ADR-0009).

	Args:
		registry: Engine of the shared registry database.
		data_dir: The gateway data directory.
		api_key: The key the caller presented.
		instance: Application instance the caller speaks for (ADR-0025);
			blank — the default instance, which is what a single-instance
			application always uses.
	"""
	key_hash = hash_api_key(api_key)
	user = await registry_ops.resolve_api_key(registry, key_hash)
	if user is None or user.status != "active":
		return None
	engine = create_sqlite_engine(user_db_path(data_dir, user.directory))
	app_row = None
	try:
		app_row = await user_apps.find_application_by_key_hash(engine, key_hash)
	finally:
		if app_row is None:
			await engine.dispose()
	if app_row is None:
		return None
	return AppContext(
		user_id=user.id,
		application_id=app_row.id,
		application_name=app_row.name,
		instance=instance.strip(),
		engine=engine,
	)


async def _get_or_create_app_user(ctx: AppContext, external_id: str) -> int:
	"""Implicit application-user registration (ADR-0011), inside the caller's instance."""
	found = await user_apps.get_app_user_id(
		ctx.engine,
		app_id=ctx.application_id,
		instance=ctx.instance,
		external_id=external_id,
	)
	if found is not None:
		return found
	try:
		return await user_apps.insert_app_user(
			ctx.engine,
			app_id=ctx.application_id,
			instance=ctx.instance,
			external_id=external_id,
		)
	except IntegrityError:
		found = await user_apps.get_app_user_id(
			ctx.engine,
			app_id=ctx.application_id,
			instance=ctx.instance,
			external_id=external_id,
		)
		if found is None:
			raise RuntimeError(f"app user {external_id!r} vanished after insert") from None
		return found


async def _find_app_user(ctx: AppContext, external_id: str) -> int:
	"""The application user's internal id, or an unknown-user error.

	Поиск идёт в экземпляре вызывающего: чужой экземпляр для него не
	существует, а не «запрещён» (ADR-0025).
	"""
	found = await user_apps.get_app_user_id(
		ctx.engine,
		app_id=ctx.application_id,
		instance=ctx.instance,
		external_id=external_id,
	)
	if found is None:
		raise OperationError("unknown_app_user", f"application user {external_id!r} is unknown")
	return found


async def ensure_bindings(
	ctx: AppContext, external_id: str, networks: list[str] | str
) -> list[dict]:
	"""Create (idempotently) and return the user's bindings for the networks.

	Args:
		ctx: The resolved application.
		external_id: The application's own user identifier (opaque string).
		networks: Network codes, or ``"all"`` for every configured network.

	Returns:
		Binding descriptions: network, address, memo.

	Raises:
		OperationError: If a requested network is not configured for the
			application, or nothing is configured at all.
	"""
	mapping = await user_apps.network_wallet_map(ctx.engine, ctx.application_id)
	if networks == "all":
		targets = sorted(mapping)
	else:
		targets = list(networks)
		for network in targets:
			if network not in mapping:
				raise OperationError(
					"network_not_configured",
					f"network {network!r} is not configured for this application",
				)
	if not targets:
		raise OperationError(
			"network_not_configured", "no networks are configured for this application"
		)

	app_user_id = await _get_or_create_app_user(ctx, external_id)
	results = []
	for network in targets:
		results.append(await _ensure_one_binding(ctx, network, mapping[network], app_user_id))
	return results


# Замки выдачи индексов деривации, по одному на пользователя: чтение максимума
# и вставка привязки обязаны быть атомарными, иначе два конкурентных запроса
# получают один индекс — и один адрес на двух плательщиков. Бэкенд — один
# процесс (ADR-0003), поэтому замок событийного цикла закрывает гонку целиком;
# уникальность (wallet, network, index) в схеме — страховка на случай иного.
_binding_locks: dict[int, asyncio.Lock] = {}


def _binding_lock(user_id: int) -> asyncio.Lock:
	"""The per-user lock serializing derivation-index allocation."""
	return _binding_locks.setdefault(user_id, asyncio.Lock())


def _binding_json(row) -> dict:
	return {"network": row.network, "address": row.address, "memo": row.memo}


async def _ensure_one_binding(
	ctx: AppContext, network: str, wallet_id: int, app_user_id: int
) -> dict:
	"""One idempotent binding: reuse, or derive the address and insert."""
	row = await user_store.get_binding(
		ctx.engine, network=network, application_id=ctx.application_id, app_user_id=app_user_id
	)
	if row is not None:
		return _binding_json(row)

	async with _binding_lock(ctx.user_id):
		# Перечитать под замком: привязка могла появиться, пока ждали очередь.
		row = await user_store.get_binding(
			ctx.engine,
			network=network,
			application_id=ctx.application_id,
			app_user_id=app_user_id,
		)
		if row is not None:
			return _binding_json(row)

		wallet = await user_wallets.get_wallet(ctx.engine, wallet_id)
		if wallet is None:
			raise OperationError("wallet_missing", f"wallet {wallet_id} does not exist")
		index = await user_store.reused_derivation_index(
			ctx.engine,
			wallet_id=wallet_id,
			application_id=ctx.application_id,
			app_user_id=app_user_id,
		)
		if index is None:
			index = await user_store.next_derivation_index(ctx.engine, wallet_id)

		address = derive_address(Family(wallet.family), wallet.xpub, index)
		try:
			await user_store.insert_binding(
				ctx.engine,
				wallet_id=wallet_id,
				network=network,
				address=address,
				application_id=ctx.application_id,
				app_user_id=app_user_id,
				derivation_index=index,
			)
		except IntegrityError as exc:
			# Под замком конфликт возможен только от внешнего по отношению
			# к процессу писателя; идемпотентный исход — вернуть свою
			# привязку, если её успели создать, иначе поднять с контекстом.
			row = await user_store.get_binding(
				ctx.engine,
				network=network,
				application_id=ctx.application_id,
				app_user_id=app_user_id,
			)
			if row is None:
				raise RuntimeError(
					f"binding insert conflicted for network {network}, "
					f"wallet {wallet_id}, index {index}: {exc.orig}"
				) from exc
			return _binding_json(row)
	return {"network": network, "address": address, "memo": ""}


async def list_addresses(
	ctx: AppContext, external_id: str, network: str | None = None
) -> list[dict]:
	"""The user's existing bindings; read-only counterpart of ensure_bindings."""
	app_user_id = await _find_app_user(ctx, external_id)
	rows = await user_store.list_bindings_of_app_user(
		ctx.engine, app_user_id, network=network
	)
	return [_binding_json(row) for row in rows]


async def _user_addresses(
	ctx: AppContext, external_id: str, network: str | None
) -> dict[str, str]:
	"""address → network of the application user's bindings."""
	app_user_id = await _find_app_user(ctx, external_id)
	rows = await user_store.list_bindings_of_app_user(
		ctx.engine, app_user_id, network=network
	)
	return {row.address: row.network for row in rows}


async def _asset_infos(registry: AsyncEngine, asset_ids: set[int]) -> dict[int, dict]:
	"""asset id → description dict of the Application API responses."""
	records = await registry_ops.get_assets_by_ids(registry, asset_ids)
	return {
		asset_id: {
			"network": record.network,
			"contract_address": record.contract_address,
			"symbol": record.symbol,
			"decimals": record.decimals,
			"kind": record.kind,
		}
		for asset_id, record in records.items()
	}


def _asset_matches(info: dict, asset_filter: str | None) -> bool:
	"""Asset filter: a contract address, or the literal ``native``."""
	if asset_filter is None:
		return True
	if asset_filter == "native":
		return info["contract_address"] == ""
	return info["contract_address"] == asset_filter


async def get_balances(
	registry: AsyncEngine,
	ctx: AppContext,
	external_id: str,
	network: str | None = None,
	asset: str | None = None,
) -> list[dict]:
	"""Balances per network + asset: total received and pending (ADR-0011).

	The current address balance is deliberately not exposed to applications.
	"""
	addresses = await _user_addresses(ctx, external_id, network)
	if not addresses:
		return []
	address_list = list(addresses)
	balance_rows = await user_views.list_balances(ctx.engine, addresses=address_list)
	pending_rows = await user_views.pending_incoming(ctx.engine, addresses=address_list)

	totals: dict[tuple[str, int], dict[str, int]] = {}
	for row in balance_rows:
		key = (addresses[row.address], row.asset_id)
		entry = totals.setdefault(key, {"received": 0, "pending": 0})
		entry["received"] += int(row.total_received)
	for row in pending_rows:
		key = (addresses[row.address], row.asset_id)
		entry = totals.setdefault(key, {"received": 0, "pending": 0})
		entry["pending"] += int(row.amount)

	infos = await _asset_infos(registry, {asset_id for (_, asset_id) in totals})
	result = []
	for (net, asset_id), entry in sorted(totals.items()):
		info = infos.get(asset_id)
		if info is None or not _asset_matches(info, asset):
			continue
		result.append(
			{
				"network": net,
				"asset": info,
				"total_received": str(entry["received"]),
				"pending": str(entry["pending"]),
			}
		)
	return result


async def get_history(
	registry: AsyncEngine,
	ctx: AppContext,
	external_id: str,
	network: str | None = None,
	asset: str | None = None,
	status: str = "confirmed",
	limit: int = DEFAULT_PAGE_LIMIT,
) -> list[dict]:
	"""Incoming transaction history with filters and pagination (ADR-0011).

	Status semantics (ADR-0017): ``confirmed`` — applied to the balance;
	``pending`` — recorded but not applied yet; ``failed`` — execution failed.
	"""
	if status not in HISTORY_STATUS_FILTERS:
		raise OperationError(
			"invalid_status", f"status must be one of {', '.join(HISTORY_STATUS_FILTERS)}"
		)
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")
	addresses = await _user_addresses(ctx, external_id, network)
	if not addresses:
		return []
	rows = await user_views.list_incoming(ctx.engine, addresses=list(addresses))

	infos = await _asset_infos(registry, {row.asset_id for row in rows})
	result = []
	for row in rows:
		info = infos.get(row.asset_id)
		if info is None or not _asset_matches(info, asset):
			continue
		api_status = classify_transaction(row.status, row.balance_applied_at)
		if status != API_STATUS_ALL and api_status != status:
			continue
		result.append(
			{
				"txid": row.txid,
				"network": addresses[row.address],
				"address": row.address,
				"asset": info,
				"amount": str(row.amount),
				"block_number": row.block_number,
				"tx_time": row.tx_time.isoformat() if row.tx_time else None,
				"status": api_status,
			}
		)
		if limit and len(result) >= limit:
			break
	return result


async def list_app_users(ctx: AppContext, limit: int = DEFAULT_PAGE_LIMIT) -> list[dict]:
	"""The application's users of the caller's instance, paginated.

	ADR-0011: default 10, 0 — all. The listing stays inside the caller's own
	instance (ADR-0025); the owner sees every instance at once in the cabinet.
	"""
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")
	rows = await user_apps.list_app_users(
		ctx.engine, ctx.application_id, instance=ctx.instance, limit=limit
	)
	return [
		{
			"external_id": row.external_id,
			"created_at": row.created_at.isoformat() if row.created_at else None,
		}
		for row in rows
	]
