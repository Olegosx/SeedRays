"""Orchestrator operations: the business core behind the API layers (ADR-0011).

The HTTP layer stays a thin adapter: every rule about bindings, balances
and history lives here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.derivation.derive import derive_address
from seedrays.families import Family
from seedrays.storage.engine import create_sqlite_engine, user_db_path
from seedrays.storage.schema_registry import api_keys, assets, users
from seedrays.storage.schema_user import (
	app_networks,
	app_users,
	applications,
	balances,
	bindings,
	transactions,
	wallets,
)

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
	engine: AsyncEngine


async def resolve_application(
	registry: AsyncEngine, data_dir: Path, api_key: str
) -> AppContext | None:
	"""Resolve an API key to its application, or None if the key is unknown.

	Lookup: the registry key index gives the owning user, the user's own
	database gives the application (ADR-0008, ADR-0009).
	"""
	key_hash = hash_api_key(api_key)
	async with registry.connect() as conn:
		key_row = (
			await conn.execute(select(api_keys).where(api_keys.c.key_hash == key_hash))
		).first()
		if key_row is None:
			return None
		user_row = (
			await conn.execute(select(users).where(users.c.id == key_row.user_id))
		).first()
	if user_row is None or user_row.status != "active":
		return None
	engine = create_sqlite_engine(user_db_path(data_dir, user_row.directory))
	app_row = None
	try:
		async with engine.connect() as conn:
			app_row = (
				await conn.execute(
					select(applications).where(applications.c.key_hash == key_hash)
				)
			).first()
	finally:
		if app_row is None:
			await engine.dispose()
	if app_row is None:
		return None
	return AppContext(
		user_id=user_row.id,
		application_id=app_row.id,
		application_name=app_row.name,
		engine=engine,
	)


async def _network_wallets(ctx: AppContext) -> dict[str, int]:
	"""The application's configured "network → wallet" mapping."""
	async with ctx.engine.connect() as conn:
		rows = (
			await conn.execute(
				select(app_networks).where(app_networks.c.application_id == ctx.application_id)
			)
		).all()
	return {row.network: row.wallet_id for row in rows}


async def _get_or_create_app_user(ctx: AppContext, external_id: str) -> int:
	"""Implicit application-user registration (ADR-0011)."""
	lookup = select(app_users.c.id).where(
		app_users.c.application_id == ctx.application_id,
		app_users.c.external_id == external_id,
	)
	async with ctx.engine.connect() as conn:
		row = (await conn.execute(lookup)).first()
	if row is not None:
		return row.id
	try:
		async with ctx.engine.begin() as conn:
			result = await conn.execute(
				insert(app_users).values(
					application_id=ctx.application_id, external_id=external_id
				)
			)
			return result.inserted_primary_key[0]
	except IntegrityError:
		async with ctx.engine.connect() as conn:
			row = (await conn.execute(lookup)).first()
		if row is None:
			raise RuntimeError(f"app user {external_id!r} vanished after insert") from None
		return row.id


async def _find_app_user(ctx: AppContext, external_id: str) -> int:
	"""The application user's internal id, or an unknown-user error."""
	async with ctx.engine.connect() as conn:
		row = (
			await conn.execute(
				select(app_users.c.id).where(
					app_users.c.application_id == ctx.application_id,
					app_users.c.external_id == external_id,
				)
			)
		).first()
	if row is None:
		raise OperationError("unknown_app_user", f"application user {external_id!r} is unknown")
	return row.id


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
	mapping = await _network_wallets(ctx)
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


async def _ensure_one_binding(
	ctx: AppContext, network: str, wallet_id: int, app_user_id: int
) -> dict:
	"""One idempotent binding: reuse, or derive the address and insert."""
	existing_q = select(bindings).where(
		bindings.c.network == network,
		bindings.c.application_id == ctx.application_id,
		bindings.c.app_user_id == app_user_id,
	)
	async with ctx.engine.connect() as conn:
		row = (await conn.execute(existing_q)).first()
	if row is not None:
		return {"network": row.network, "address": row.address, "memo": row.memo}

	async with ctx.engine.connect() as conn:
		wallet = (
			await conn.execute(select(wallets).where(wallets.c.id == wallet_id))
		).first()
		if wallet is None:
			raise OperationError("wallet_missing", f"wallet {wallet_id} does not exist")
		# Переиспользование индекса: та же связка «кошелёк + пользователь
		# приложения» в другой сети даёт тот же адрес (развилка 2, EVM-свойство).
		reuse = (
			await conn.execute(
				select(bindings.c.derivation_index)
				.where(
					bindings.c.wallet_id == wallet_id,
					bindings.c.application_id == ctx.application_id,
					bindings.c.app_user_id == app_user_id,
				)
				.limit(1)
			)
		).first()
		if reuse is not None:
			index = reuse.derivation_index
		else:
			max_index = (
				await conn.execute(
					select(func.max(bindings.c.derivation_index)).where(
						bindings.c.wallet_id == wallet_id
					)
				)
			).scalar()
			index = 0 if max_index is None else max_index + 1

	address = derive_address(Family(wallet.family), wallet.xpub, index)
	try:
		async with ctx.engine.begin() as conn:
			await conn.execute(
				insert(bindings).values(
					wallet_id=wallet_id,
					network=network,
					address=address,
					application_id=ctx.application_id,
					app_user_id=app_user_id,
					derivation_index=index,
				)
			)
	except IntegrityError:
		# Гонка создания: привязка появилась параллельно — возвращаем её.
		async with ctx.engine.connect() as conn:
			row = (await conn.execute(existing_q)).first()
		if row is None:
			raise RuntimeError(f"binding for {network} vanished after insert") from None
		return {"network": row.network, "address": row.address, "memo": row.memo}
	return {"network": network, "address": address, "memo": ""}


async def list_addresses(
	ctx: AppContext, external_id: str, network: str | None = None
) -> list[dict]:
	"""The user's existing bindings; read-only counterpart of ensure_bindings."""
	app_user_id = await _find_app_user(ctx, external_id)
	query = select(bindings).where(bindings.c.app_user_id == app_user_id)
	if network is not None:
		query = query.where(bindings.c.network == network)
	async with ctx.engine.connect() as conn:
		rows = (await conn.execute(query)).all()
	return [{"network": r.network, "address": r.address, "memo": r.memo} for r in rows]


async def _user_addresses(
	ctx: AppContext, external_id: str, network: str | None
) -> dict[str, str]:
	"""address → network of the application user's bindings."""
	app_user_id = await _find_app_user(ctx, external_id)
	query = select(bindings.c.address, bindings.c.network).where(
		bindings.c.app_user_id == app_user_id
	)
	if network is not None:
		query = query.where(bindings.c.network == network)
	async with ctx.engine.connect() as conn:
		rows = (await conn.execute(query)).all()
	return {row.address: row.network for row in rows}


async def _asset_infos(registry: AsyncEngine, asset_ids: set[int]) -> dict[int, dict]:
	"""asset id → description from the registry catalog."""
	if not asset_ids:
		return {}
	async with registry.connect() as conn:
		rows = (await conn.execute(select(assets).where(assets.c.id.in_(asset_ids)))).all()
	return {
		row.id: {
			"network": row.network,
			"contract_address": row.contract_address,
			"symbol": row.symbol,
			"decimals": row.decimals,
			"kind": row.kind,
		}
		for row in rows
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
	async with ctx.engine.connect() as conn:
		balance_rows = (
			await conn.execute(select(balances).where(balances.c.address.in_(address_list)))
		).all()
		pending_rows = (
			await conn.execute(
				select(
					transactions.c.address,
					transactions.c.asset_id,
					transactions.c.amount,
				).where(
					transactions.c.address.in_(address_list),
					transactions.c.direction == "in",
					transactions.c.status == "success",
					transactions.c.balance_applied_at.is_(None),
				)
			)
		).all()

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


_HISTORY_STATUSES = ("confirmed", "pending", "failed", "all")


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
	if status not in _HISTORY_STATUSES:
		raise OperationError(
			"invalid_status", f"status must be one of {', '.join(_HISTORY_STATUSES)}"
		)
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")
	addresses = await _user_addresses(ctx, external_id, network)
	if not addresses:
		return []
	query = (
		select(transactions)
		.where(
			transactions.c.address.in_(list(addresses)),
			transactions.c.direction == "in",
		)
		.order_by(transactions.c.block_number.desc(), transactions.c.id.desc())
	)
	if status == "confirmed":
		query = query.where(
			transactions.c.status == "success",
			transactions.c.balance_applied_at.is_not(None),
		)
	elif status == "pending":
		query = query.where(
			transactions.c.status == "success",
			transactions.c.balance_applied_at.is_(None),
		)
	elif status == "failed":
		query = query.where(transactions.c.status == "failed")
	async with ctx.engine.connect() as conn:
		rows = (await conn.execute(query)).all()

	infos = await _asset_infos(registry, {row.asset_id for row in rows})
	result = []
	for row in rows:
		info = infos.get(row.asset_id)
		if info is None or not _asset_matches(info, asset):
			continue
		if row.status == "failed":
			api_status = "failed"
		elif row.balance_applied_at is not None:
			api_status = "confirmed"
		else:
			api_status = "pending"
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
	"""The application's users, paginated (ADR-0011: default 10, 0 — all)."""
	if limit < 0:
		raise OperationError("invalid_limit", "limit must be non-negative (0 means all)")
	query = (
		select(app_users)
		.where(app_users.c.application_id == ctx.application_id)
		.order_by(app_users.c.id)
	)
	if limit:
		query = query.limit(limit)
	async with ctx.engine.connect() as conn:
		rows = (await conn.execute(query)).all()
	return [
		{
			"external_id": row.external_id,
			"created_at": row.created_at.isoformat() if row.created_at else None,
		}
		for row in rows
	]
