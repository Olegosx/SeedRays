"""Application management operations of the user cabinet.

An application is identified by its API key (ADR-0009): the key itself is
never stored — the user database keeps its SHA-256 fingerprint and the
open first characters for identification, the registry keeps the
"fingerprint → owner" index (ADR-0008). The raw key is returned exactly
once at creation or reissue.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.orchestrator.operations import OperationError, hash_api_key
from seedrays.storage.schema_registry import api_keys
from seedrays.storage.schema_user import app_networks, app_users, applications, bindings, wallets

KEY_PREFIX_LEN = 9  # "srk_" + 5 знаков — достаточно для опознания


def _now() -> datetime:
	return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_key() -> str:
	return "srk_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class AppSummary:
	"""One application in the list."""

	id: int
	name: str
	networks: list[str]
	users: int
	key_prefix: str
	key_issued_at: datetime | None
	key_revoked: bool
	created_at: datetime | None


async def list_applications(engine: AsyncEngine) -> list[AppSummary]:
	"""The user's applications with networks and user counts."""
	async with engine.connect() as conn:
		rows = (await conn.execute(select(applications).order_by(applications.c.id))).all()
		networks = (await conn.execute(select(app_networks))).all()
		counts = dict(
			(
				await conn.execute(
					select(app_users.c.application_id, func.count())
					.group_by(app_users.c.application_id)
				)
			).all()
		)
	per_app: dict[int, list[str]] = {}
	for mapping in networks:
		per_app.setdefault(mapping.application_id, []).append(mapping.network)
	return [
		AppSummary(
			id=row.id,
			name=row.name,
			networks=sorted(per_app.get(row.id, [])),
			users=counts.get(row.id, 0),
			key_prefix=row.key_prefix,
			key_issued_at=row.key_issued_at,
			key_revoked=row.key_hash is None,
			created_at=row.created_at,
		)
		for row in rows
	]


async def _get_summary(engine: AsyncEngine, app_id: int) -> AppSummary:
	for summary in await list_applications(engine):
		if summary.id == app_id:
			return summary
	raise OperationError("unknown_application", f"application {app_id} does not exist")


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
	async with engine.begin() as conn:
		result = await conn.execute(
			insert(applications).values(
				name=name,
				key_hash=key_hash,
				key_prefix=key[:KEY_PREFIX_LEN],
				key_issued_at=_now(),
			)
		)
		app_id = result.inserted_primary_key[0]
	async with registry.begin() as conn:
		await conn.execute(insert(api_keys).values(key_hash=key_hash, user_id=user_id))
	return await _get_summary(engine, app_id), key


async def reissue_key(
	registry: AsyncEngine, engine: AsyncEngine, *, user_id: int, app_id: int
) -> tuple[AppSummary, str]:
	"""Replace the application's key; the old one dies, the new one shows once."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(select(applications).where(applications.c.id == app_id))
		).first()
	if row is None:
		raise OperationError("unknown_application", f"application {app_id} does not exist")
	key = _new_key()
	key_hash = hash_api_key(key)
	async with engine.begin() as conn:
		await conn.execute(
			update(applications)
			.where(applications.c.id == app_id)
			.values(key_hash=key_hash, key_prefix=key[:KEY_PREFIX_LEN], key_issued_at=_now())
		)
	async with registry.begin() as conn:
		if row.key_hash is not None:
			await conn.execute(delete(api_keys).where(api_keys.c.key_hash == row.key_hash))
		await conn.execute(insert(api_keys).values(key_hash=key_hash, user_id=user_id))
	return await _get_summary(engine, app_id), key


async def revoke_key(registry: AsyncEngine, engine: AsyncEngine, *, app_id: int) -> AppSummary:
	"""Revoke the application's key: the application loses API access."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(select(applications).where(applications.c.id == app_id))
		).first()
	if row is None:
		raise OperationError("unknown_application", f"application {app_id} does not exist")
	async with engine.begin() as conn:
		await conn.execute(
			update(applications).where(applications.c.id == app_id).values(key_hash=None)
		)
	if row.key_hash is not None:
		async with registry.begin() as conn:
			await conn.execute(delete(api_keys).where(api_keys.c.key_hash == row.key_hash))
	return await _get_summary(engine, app_id)


@dataclass(frozen=True)
class AppDetail:
	"""The application page: summary + mappings + users."""

	summary: AppSummary
	mappings: list[dict]
	users: list[dict]


async def get_application(engine: AsyncEngine, app_id: int) -> AppDetail:
	"""The application with its network mappings and users."""
	summary = await _get_summary(engine, app_id)
	async with engine.connect() as conn:
		mapping_rows = (
			await conn.execute(
				select(app_networks.c.network, app_networks.c.wallet_id, wallets.c.label)
				.join(wallets, wallets.c.id == app_networks.c.wallet_id)
				.where(app_networks.c.application_id == app_id)
				.order_by(app_networks.c.network)
			)
		).all()
		user_rows = (
			await conn.execute(
				select(app_users)
				.where(app_users.c.application_id == app_id)
				.order_by(app_users.c.id)
			)
		).all()
		address_counts = dict(
			(
				await conn.execute(
					select(bindings.c.app_user_id, func.count())
					.where(bindings.c.application_id == app_id)
					.group_by(bindings.c.app_user_id)
				)
			).all()
		)
	return AppDetail(
		summary=summary,
		mappings=[
			{"network": m.network, "wallet_id": m.wallet_id, "wallet_label": m.label}
			for m in mapping_rows
		],
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
		OperationError: unknown_application / wallet_missing.
	"""
	await _get_summary(engine, app_id)
	async with engine.connect() as conn:
		wallet = (
			await conn.execute(select(wallets.c.id).where(wallets.c.id == wallet_id))
		).first()
	if wallet is None:
		raise OperationError("wallet_missing", f"wallet {wallet_id} does not exist")
	async with engine.begin() as conn:
		result = await conn.execute(
			update(app_networks)
			.where(
				app_networks.c.application_id == app_id, app_networks.c.network == network
			)
			.values(wallet_id=wallet_id)
		)
		if result.rowcount == 0:
			await conn.execute(
				insert(app_networks).values(
					application_id=app_id, network=network, wallet_id=wallet_id
				)
			)


async def remove_network_mapping(engine: AsyncEngine, *, app_id: int, network: str) -> None:
	"""Drop one mapping entry; existing bindings of that network stay untouched."""
	async with engine.begin() as conn:
		await conn.execute(
			delete(app_networks).where(
				app_networks.c.application_id == app_id, app_networks.c.network == network
			)
		)
