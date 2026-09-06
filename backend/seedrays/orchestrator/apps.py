"""Application management operations of the user cabinet.

An application is identified by its API key (ADR-0009): the key itself is
never stored — the user database keeps its SHA-256 fingerprint and the
open first characters for identification, the registry keeps the
"fingerprint → owner" index (ADR-0008). The raw key is returned exactly
once at creation or reissue. Database access goes through the storage
layer (ADR-0006).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.orchestrator.operations import OperationError, hash_api_key
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_apps, user_wallets
from seedrays.storage.engine import now_utc
from seedrays.storage.user_apps import AppSummary

logger = logging.getLogger(__name__)

KEY_PREFIX_LEN = 9  # "srk_" + 5 знаков — достаточно для опознания


def _new_key() -> str:
	return "srk_" + secrets.token_urlsafe(32)


async def list_applications(engine: AsyncEngine) -> list[AppSummary]:
	"""The user's applications with networks and user counts."""
	return await user_apps.list_summaries(engine)


async def _require_summary(engine: AsyncEngine, app_id: int) -> AppSummary:
	summary = await user_apps.get_summary(engine, app_id)
	if summary is None:
		raise OperationError("unknown_application", f"application {app_id} does not exist")
	return summary


async def create_application(
	registry: AsyncEngine, engine: AsyncEngine, *, user_id: int, name: str
) -> tuple[AppSummary, str]:
	"""Create an application and issue its key (returned once).

	Raises:
		OperationError: invalid_name.
	"""
	name = name.strip()
	if not 1 <= len(name) <= 64:
		raise OperationError("invalid_name", "the application name must be 1-64 characters")
	key = _new_key()
	key_hash = hash_api_key(key)
	app_id = await user_apps.insert_application(
		engine,
		name=name,
		key_hash=key_hash,
		key_prefix=key[:KEY_PREFIX_LEN],
		issued_at=now_utc(),
	)
	try:
		await registry_ops.add_api_key(registry, user_id=user_id, key_hash=key_hash)
	except BaseException:
		# Компенсация: без строки индекса ключ не работает — не отдавать же
		# пользователю «выданный» мёртвый ключ; создание отменяется целиком.
		logger.exception("api-key index write failed; rolling back application %d", app_id)
		await user_apps.delete_application(engine, app_id)
		raise
	return await _require_summary(engine, app_id), key


async def reissue_key(
	registry: AsyncEngine, engine: AsyncEngine, *, user_id: int, app_id: int
) -> tuple[AppSummary, str]:
	"""Replace the application's key; the old one dies, the new one shows once."""
	row = await user_apps.get_application(engine, app_id)
	if row is None:
		raise OperationError("unknown_application", f"application {app_id} does not exist")
	key = _new_key()
	key_hash = hash_api_key(key)
	await user_apps.set_application_key(
		engine, app_id, key_hash=key_hash, key_prefix=key[:KEY_PREFIX_LEN], issued_at=now_utc()
	)
	try:
		if row.key_hash is not None:
			await registry_ops.delete_api_key(registry, row.key_hash)
		await registry_ops.add_api_key(registry, user_id=user_id, key_hash=key_hash)
	except BaseException:
		# Компенсация: индекс не переключился — возвращаем прежний ключ,
		# иначе пользователь остаётся с нерабочим «новым» ключом.
		logger.exception(
			"api-key index update failed; restoring the previous key of application %d",
			app_id,
		)
		await user_apps.set_application_key(
			engine,
			app_id,
			key_hash=row.key_hash,
			key_prefix=row.key_prefix,
			issued_at=row.key_issued_at,
		)
		raise
	return await _require_summary(engine, app_id), key


async def revoke_key(registry: AsyncEngine, engine: AsyncEngine, *, app_id: int) -> AppSummary:
	"""Revoke the application's key: the application loses API access."""
	row = await user_apps.get_application(engine, app_id)
	if row is None:
		raise OperationError("unknown_application", f"application {app_id} does not exist")
	await user_apps.set_application_key(engine, app_id, key_hash=None)
	if row.key_hash is not None:
		await registry_ops.delete_api_key(registry, row.key_hash)
	return await _require_summary(engine, app_id)


@dataclass(frozen=True)
class AppDetail:
	"""The application page: summary + mappings + users."""

	summary: AppSummary
	mappings: list[dict]
	users: list[dict]


async def get_application(engine: AsyncEngine, app_id: int) -> AppDetail:
	"""The application with its network mappings and users."""
	summary = await _require_summary(engine, app_id)
	mappings = await user_apps.list_network_mappings(engine, app_id)
	user_rows = await user_apps.list_app_users(engine, app_id)
	address_counts = await user_apps.app_user_address_counts(engine, app_id)
	return AppDetail(
		summary=summary,
		mappings=mappings,
		users=[
			{
				"external_id": u.external_id,
				"addresses": address_counts.get(u.id, 0),
				"created_at": u.created_at.isoformat() if u.created_at else None,
			}
			for u in user_rows
		],
	)


async def set_network_mapping(
	engine: AsyncEngine, *, app_id: int, network: str, wallet_id: int
) -> None:
	"""Create or replace the application's "network → wallet" mapping entry.

	Raises:
		OperationError: unknown_application / unknown_wallet.
	"""
	await _require_summary(engine, app_id)
	if await user_wallets.get_wallet(engine, wallet_id) is None:
		# Ошибка ввода пользователя кабинета, не серверная несогласованность.
		raise OperationError("unknown_wallet", f"wallet {wallet_id} does not exist")
	await user_apps.upsert_network_mapping(
		engine, app_id=app_id, network=network, wallet_id=wallet_id
	)


async def remove_network_mapping(engine: AsyncEngine, *, app_id: int, network: str) -> None:
	"""Drop one mapping entry; existing bindings of that network stay untouched."""
	await user_apps.delete_network_mapping(engine, app_id=app_id, network=network)
