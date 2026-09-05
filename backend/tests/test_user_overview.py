"""Cabinet history and dashboard summary routes."""

import asyncio
from datetime import datetime
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.orchestrator.overview import format_amount
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_registry

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)


def test_format_amount_is_exact() -> None:
	"""Integer maths only: no floats, trimmed zeros, sign preserved."""
	assert format_amount(1_000_000, 6) == "1"
	assert format_amount(990_500_000, 6) == "990.5"
	assert format_amount(1, 6) == "0.000001"
	assert format_amount(0, 6) == "0"
	assert format_amount(-1_500_000, 6) == "-1.5"
	assert format_amount(7, 0) == "7"


async def _prepared_client(data_dir: Path) -> tuple[httpx.AsyncClient, str, str]:
	"""A signed-in client with a wallet, an app, a mapping and one binding."""
	upgrade_registry(data_dir)
	transport = httpx.ASGITransport(app=create_app(data_dir, mailer=None))
	client = httpx.AsyncClient(transport=transport, base_url="http://gw")
	await client.post(
		"/v1/user/register",
		json={"username": "alice", "email": "a@example.com", "password": "correct-horse"},
	)
	login = await client.post(
		"/v1/user/login", json={"identifier": "alice", "password": "correct-horse"}
	)
	csrf = login.json()["csrf"]
	headers = {"X-CSRF-Token": csrf}
	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	wallet = await client.post(
		"/v1/user/wallets",
		json={"family": "tron", "xpub": xpub, "label": "Main"},
		headers=headers,
	)
	app = await client.post("/v1/user/applications", json={"name": "Shop"}, headers=headers)
	await client.put(
		f"/v1/user/applications/{app.json()['application']['id']}/networks",
		json={"network": "tron-nile", "wallet_id": wallet.json()["wallet"]["id"]},
		headers=headers,
	)
	issued = await client.post(
		"/v1/app/users/u1/addresses",
		json={"networks": "all"},
		headers={"X-API-Key": app.json()["key"]},
	)
	return client, csrf, issued.json()["addresses"][0]["address"]


async def _seed_transactions(data_dir: Path, address: str) -> None:
	"""One applied and one pending USDT transfer on the bound address."""
	registry = create_sqlite_engine(registry_db_path(data_dir))
	asset = await registry_ops.get_or_create_asset(
		registry, network="tron-nile", kind="token", contract_address="C1",
		symbol="USDT", decimals=6,
	)
	await registry.dispose()
	engine = create_sqlite_engine(user_db_path(data_dir, "u1"))
	for txid, block in (("tx-old", 90), ("tx-new", 105)):
		await user_store.record_transaction(
			engine,
			address=address,
			txid=txid,
			asset_id=asset.id,
			direction="in",
			amount=1_500_000,
			block_number=block,
			tx_time=datetime(2026, 9, 3, 12, 0),
			status="success",
		)
	await user_store.apply_finalized(
		engine, asset_ids={asset.id}, boundary_block=100,
		applied_at=datetime(2026, 9, 3, 12, 1),
	)
	await engine.dispose()


def test_history_and_overview(tmp_path: Path) -> None:
	"""History rows carry wallet/network/asset/status; overview aggregates them."""

	async def scenario() -> None:
		client, _csrf, address = await _prepared_client(tmp_path)
		try:
			await _seed_transactions(tmp_path, address)

			everything = await client.get("/v1/user/history")
			rows = everything.json()["history"]
			assert [r["txid"] for r in rows] == ["tx-new", "tx-old"]
			assert rows[0]["status"] == "pending"
			assert rows[1]["status"] == "confirmed"
			assert rows[0]["wallet"] == "Main"
			assert rows[0]["network"] == "tron-nile"
			assert rows[0]["amount"] == "1.5"

			confirmed = await client.get("/v1/user/history", params={"status": "confirmed"})
			assert [r["txid"] for r in confirmed.json()["history"]] == ["tx-old"]

			data = (await client.get("/v1/user/overview")).json()
			assert data["counters"] == {"wallets": 1, "applications": 1, "addresses": 1}
			assert data["receipts"] == [
				{"network": "tron-nile", "asset": "USDT", "received": "1.5", "pending": "1.5"}
			]
			assert len(data["recent"]) == 2
		finally:
			await client.aclose()

	asyncio.run(scenario())
