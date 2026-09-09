"""User-database operations: applications, their keys, networks and users.

The raw API key never reaches this module — only its SHA-256 fingerprint
and the open prefix are stored (ADR-0009); the gateway-wide
"fingerprint → owner" index lives in the registry (ADR-0008).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage.engine import upsert
from seedrays.storage.schema_user import app_networks, app_users, applications, bindings, wallets


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


def _summary(row, networks: list[str], users: int) -> AppSummary:
	return AppSummary(
		id=row.id,
		name=row.name,
		networks=sorted(networks),
		users=users,
		key_prefix=row.key_prefix,
		key_issued_at=row.key_issued_at,
		key_revoked=row.key_hash is None,
		created_at=row.created_at,
	)


async def list_summaries(engine: AsyncEngine) -> list[AppSummary]:
	"""Every application with its networks and user count (the list screen)."""
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
	return [_summary(row, per_app.get(row.id, []), counts.get(row.id, 0)) for row in rows]


async def get_summary(engine: AsyncEngine, app_id: int) -> AppSummary | None:
	"""One application's summary by id (point read), or None."""
	async with engine.connect() as conn:
		row = (
			await conn.execute(select(applications).where(applications.c.id == app_id))
		).first()
		if row is None:
			return None
		networks = [
			m.network
			for m in (
				await conn.execute(
					select(app_networks.c.network).where(
						app_networks.c.application_id == app_id
					)
				)
			).all()
		]
		users = (
			await conn.execute(
				select(func.count()).select_from(app_users).where(
					app_users.c.application_id == app_id
				)
			)
		).scalar()
	return _summary(row, networks, users or 0)


async def get_application(engine: AsyncEngine, app_id: int):
	"""The raw application row by id, or None (id, key_hash, prefix, times)."""
	async with engine.connect() as conn:
		return (
			await conn.execute(select(applications).where(applications.c.id == app_id))
		).first()


async def insert_application(
	engine: AsyncEngine, *, name: str, key_hash: str, key_prefix: str, issued_at: datetime
) -> int:
	"""Create an application row with its first key fingerprint; returns the id."""
	async with engine.begin() as conn:
		result = await conn.execute(
			insert(applications).values(
				name=name, key_hash=key_hash, key_prefix=key_prefix, key_issued_at=issued_at
			)
		)
		return result.inserted_primary_key[0]


async def delete_application(engine: AsyncEngine, app_id: int) -> None:
	"""Remove an application row (compensation for a failed creation)."""
	async with engine.begin() as conn:
		await conn.execute(delete(applications).where(applications.c.id == app_id))


async def set_application_key(
	engine: AsyncEngine,
	app_id: int,
	*,
	key_hash: str | None,
	key_prefix: str | None = None,
	issued_at: datetime | None = None,
) -> None:
	"""Replace the key fingerprint (reissue) or clear it (revocation).

	With ``key_hash=None`` only the hash is cleared; prefix and issue time
	stay for identification of the revoked key.
	"""
	values: dict = {"key_hash": key_hash}
	if key_prefix is not None:
		values["key_prefix"] = key_prefix
	if issued_at is not None:
		values["key_issued_at"] = issued_at
	async with engine.begin() as conn:
		await conn.execute(
			update(applications).where(applications.c.id == app_id).values(**values)
		)


async def list_network_mappings(engine: AsyncEngine, app_id: int) -> list[dict]:
	"""The application's "network → wallet" entries with wallet labels."""
	async with engine.connect() as conn:
		rows = (
			await conn.execute(
				select(app_networks.c.network, app_networks.c.wallet_id, wallets.c.label)
				.join(wallets, wallets.c.id == app_networks.c.wallet_id)
				.where(app_networks.c.application_id == app_id)
				.order_by(app_networks.c.network)
			)
		).all()
	return [
		{"network": m.network, "wallet_id": m.wallet_id, "wallet_label": m.label}
		for m in rows
	]


async def upsert_network_mapping(
	engine: AsyncEngine, *, app_id: int, network: str, wallet_id: int
) -> None:
	"""Create or replace one "network → wallet" mapping entry."""
	async with engine.begin() as conn:
		await upsert(
			conn,
			app_networks,
			{"application_id": app_id, "network": network},
			{"wallet_id": wallet_id},
		)


async def delete_network_mapping(engine: AsyncEngine, *, app_id: int, network: str) -> None:
	"""Drop one mapping entry."""
	async with engine.begin() as conn:
		await conn.execute(
			delete(app_networks).where(
				app_networks.c.application_id == app_id, app_networks.c.network == network
			)
		)


async def network_wallet_map(engine: AsyncEngine, app_id: int) -> dict[str, int]:
	"""network → wallet id of one application."""
	async with engine.connect() as conn:
		rows = (
			await conn.execute(
				select(app_networks).where(app_networks.c.application_id == app_id)
			)
		).all()
	return {row.network: row.wallet_id for row in rows}


async def get_app_user_id(
	engine: AsyncEngine, *, app_id: int, instance: str, external_id: str
) -> int | None:
	"""The internal id of an application user, or None.

	Args:
		engine: The owner's user-database engine.
		app_id: The application the user belongs to.
		instance: Application instance — the namespace of external ids
			(ADR-0025); the empty string is the default instance.
		external_id: The application's own user identifier.
	"""
	async with engine.connect() as conn:
		row = (
			await conn.execute(
				select(app_users.c.id).where(
					app_users.c.application_id == app_id,
					app_users.c.instance == instance,
					app_users.c.external_id == external_id,
				)
			)
		).first()
	return None if row is None else row.id


async def insert_app_user(
	engine: AsyncEngine, *, app_id: int, instance: str, external_id: str
) -> int:
	"""Create an application user row; returns its id.

	Raises:
		IntegrityError: On a concurrent insert of the same identity —
			the caller re-reads and reuses the winner.
	"""
	async with engine.begin() as conn:
		result = await conn.execute(
			insert(app_users).values(
				application_id=app_id, instance=instance, external_id=external_id
			)
		)
		return result.inserted_primary_key[0]


async def list_app_users(
	engine: AsyncEngine, app_id: int, *, instance: str | None = None, limit: int = 0
) -> list:
	"""Application user rows, oldest first; ``limit=0`` — all.

	Args:
		engine: The owner's user-database engine.
		app_id: The application to list.
		instance: One instance's namespace, or None for every instance —
			the cabinet shows the owner all of them, the Application API
			stays inside the caller's own instance (ADR-0025).
		limit: Page size; 0 means everything.
	"""
	query = (
		select(app_users)
		.where(app_users.c.application_id == app_id)
		.order_by(app_users.c.id)
	)
	if instance is not None:
		query = query.where(app_users.c.instance == instance)
	if limit:
		query = query.limit(limit)
	async with engine.connect() as conn:
		return (await conn.execute(query)).all()


async def app_user_address_counts(engine: AsyncEngine, app_id: int) -> dict[int, int]:
	"""app_user id → number of bound addresses (the application page)."""
	async with engine.connect() as conn:
		return dict(
			(
				await conn.execute(
					select(bindings.c.app_user_id, func.count())
					.where(bindings.c.application_id == app_id)
					.group_by(bindings.c.app_user_id)
				)
			).all()
		)


async def find_application_by_key_hash(engine: AsyncEngine, key_hash: str):
	"""The application row owning a key fingerprint, or None."""
	async with engine.connect() as conn:
		return (
			await conn.execute(
				select(applications).where(applications.c.key_hash == key_hash)
			)
		).first()
