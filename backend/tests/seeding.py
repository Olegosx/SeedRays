"""Shared test seeding: a gateway with one user, wallet, application and key."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import insert

from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.orchestrator.operations import hash_api_key
from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_registry, schema_user
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_registry

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)
TEST_API_KEY = "test-api-key-0001"
# m/44'/195'/0'/0/0 этой фразы — эталонный адрес из тестов деривации.
FIRST_TRON_ADDRESS = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"


async def seed_gateway(data_dir: Path, networks: tuple[str, ...] = ("tron-nile",)) -> None:
	"""Create a migrated gateway with one user, wallet, application and API key.

	The wallet is the reference test wallet (TRON family), the application
	is mapped to the given networks and authenticated by TEST_API_KEY.
	"""
	upgrade_registry(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	user = await registry_ops.create_user(registry, data_dir, "alice", "password-hash")

	key_hash = hash_api_key(TEST_API_KEY)
	async with registry.begin() as conn:
		await conn.execute(
			insert(schema_registry.api_keys).values(key_hash=key_hash, user_id=user.id)
		)
	await registry.dispose()

	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	engine = create_sqlite_engine(user_db_path(data_dir, user.directory))
	async with engine.begin() as conn:
		await conn.execute(insert(schema_user.wallets).values(family="tron", xpub=xpub))
		await conn.execute(
			insert(schema_user.applications).values(name="shop", key_hash=key_hash)
		)
		for network in networks:
			await conn.execute(
				insert(schema_user.app_networks).values(
					application_id=1, network=network, wallet_id=1
				)
			)
	await engine.dispose()
