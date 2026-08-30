"""Application API tests over the ASGI transport; no network, real databases."""

import asyncio
from datetime import datetime
from pathlib import Path

import httpx

from seeding import FIRST_TRON_ADDRESS, TEST_API_KEY, seed_gateway
from seedrays.api.app_api import create_app
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path

NETWORKS = ("tron-nile", "tron")
HEADERS = {"X-API-Key": TEST_API_KEY}


def _client(data_dir: Path) -> httpx.AsyncClient:
	transport = httpx.ASGITransport(app=create_app(data_dir))
	return httpx.AsyncClient(transport=transport, base_url="http://gw")


async def _seed_transactions(data_dir: Path) -> None:
	"""One applied (confirmed) and one unapplied (pending) incoming USDT transfer."""
	registry = create_sqlite_engine(registry_db_path(data_dir))
	asset = await registry_ops.get_or_create_asset(
		registry, network="tron-nile", kind="token", contract_address="C1", symbol="USDT", decimals=6
	)
	await registry.dispose()

	engine = create_sqlite_engine(user_db_path(data_dir, "u1"))
	for txid, block in (("tx-old", 90), ("tx-new", 105)):
		await user_store.record_transaction(
			engine,
			address=FIRST_TRON_ADDRESS,
			txid=txid,
			asset_id=asset.id,
			direction="in",
			amount=1_000_000,
			block_number=block,
			tx_time=datetime(2026, 8, 29, 12, 0),
			status="success",
		)
	await user_store.apply_finalized(
		engine,
		asset_ids={asset.id},
		boundary_block=100,
		applied_at=datetime(2026, 8, 29, 12, 1),
	)
	await engine.dispose()


def test_authentication_required(tmp_path: Path) -> None:
	"""Requests without a valid key are rejected in the unified format."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			response = await client.get("/v1/app/users")
			assert response.status_code == 401
			assert response.json()["error"]["code"] == "unauthorized"

			response = await client.get("/v1/app/users", headers={"X-API-Key": "wrong"})
			assert response.status_code == 401

	asyncio.run(scenario())


def test_address_issue_flow(tmp_path: Path) -> None:
	"""Create-addresses is idempotent, derives the reference address, reads back."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			create = await client.post(
				"/v1/app/users/user1/addresses", json={"networks": "all"}, headers=HEADERS
			)
			assert create.status_code == 200
			addresses = create.json()["addresses"]
			# Обе сети семейства TRON на одном кошельке: индекс переиспользован,
			# адрес одинаковый (развилка 2), и это эталонный адрес индекса 0.
			assert {a["network"] for a in addresses} == set(NETWORKS)
			assert {a["address"] for a in addresses} == {FIRST_TRON_ADDRESS}

			again = await client.post(
				"/v1/app/users/user1/addresses",
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			assert again.json()["addresses"] == [
				{"network": "tron-nile", "address": FIRST_TRON_ADDRESS, "memo": ""}
			]

			second_user = await client.post(
				"/v1/app/users/user2/addresses",
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			other_address = second_user.json()["addresses"][0]["address"]
			assert other_address != FIRST_TRON_ADDRESS  # следующий индекс

			read = await client.get(
				"/v1/app/users/user1/addresses",
				params={"network": "tron"},
				headers=HEADERS,
			)
			assert read.json()["addresses"] == [
				{"network": "tron", "address": FIRST_TRON_ADDRESS, "memo": ""}
			]

			missing = await client.get("/v1/app/users/nobody/addresses", headers=HEADERS)
			assert missing.status_code == 404
			assert missing.json()["error"]["code"] == "unknown_app_user"

			bad_network = await client.post(
				"/v1/app/users/user1/addresses",
				json={"networks": ["ton"]},
				headers=HEADERS,
			)
			assert bad_network.status_code == 400
			assert bad_network.json()["error"]["code"] == "network_not_configured"

			invalid = await client.post(
				"/v1/app/users/user1/addresses", json={"nets": 1}, headers=HEADERS
			)
			assert invalid.status_code == 400
			assert invalid.json()["error"]["code"] == "validation"

	asyncio.run(scenario())


def test_balances_and_history(tmp_path: Path) -> None:
	"""Balances carry received + pending as strings; history filters by status."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			await client.post(
				"/v1/app/users/user1/addresses",
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
		await _seed_transactions(tmp_path)

		async with _client(tmp_path) as client:
			balances = await client.get("/v1/app/users/user1/balances", headers=HEADERS)
			assert balances.status_code == 200
			rows = balances.json()["balances"]
			assert len(rows) == 1
			assert rows[0]["network"] == "tron-nile"
			assert rows[0]["asset"]["symbol"] == "USDT"
			assert rows[0]["total_received"] == "1000000"
			assert rows[0]["pending"] == "1000000"

			confirmed = await client.get("/v1/app/users/user1/history", headers=HEADERS)
			assert [t["txid"] for t in confirmed.json()["history"]] == ["tx-old"]
			assert confirmed.json()["history"][0]["status"] == "confirmed"

			pending = await client.get(
				"/v1/app/users/user1/history", params={"status": "pending"}, headers=HEADERS
			)
			assert [t["txid"] for t in pending.json()["history"]] == ["tx-new"]

			everything = await client.get(
				"/v1/app/users/user1/history",
				params={"status": "all", "limit": 0},
				headers=HEADERS,
			)
			assert len(everything.json()["history"]) == 2

			users = await client.get("/v1/app/users", headers=HEADERS)
			assert [u["external_id"] for u in users.json()["users"]] == ["user1"]

	asyncio.run(scenario())
