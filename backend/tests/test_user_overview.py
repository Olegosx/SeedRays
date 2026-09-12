"""Cabinet history and dashboard summary routes."""

import asyncio
from datetime import datetime
from pathlib import Path

import httpx

from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_store
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seeding import signed_in_client

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)


async def _prepared_client(data_dir: Path) -> tuple[httpx.AsyncClient, str, str]:
	"""A signed-in client with a wallet, an app, a mapping and one binding."""
	client, csrf = await signed_in_client(data_dir)
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
	# tx-old — финализирована и применяется; tx-new — предварительная (pending).
	for txid, block, finalized in (("tx-old", 90, True), ("tx-new", 105, False)):
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
			finalized_at=datetime(2026, 9, 3, 12, 1) if finalized else None,
		)
	await user_store.apply_finalized(
		engine, asset_ids={asset.id},
		applied_at=datetime(2026, 9, 3, 12, 1),
	)
	await engine.dispose()


def test_history_show_more_pagination(tmp_path: Path) -> None:
	"""The cursor continues strictly past the last row, without duplicates."""

	async def scenario() -> None:
		client, _csrf, address = await _prepared_client(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			asset = await registry_ops.get_or_create_asset(
				registry, network="tron-nile", kind="token", contract_address="C1",
				symbol="USDT", decimals=6,
			)
			await registry.dispose()
			engine = create_sqlite_engine(user_db_path(tmp_path, "u1"))
			for txid, block in (("tx-1", 90), ("tx-2", 100), ("tx-3", 110)):
				await user_store.record_transaction(
					engine,
					address=address,
					txid=txid,
					asset_id=asset.id,
					direction="in",
					amount=1_000_000,
					block_number=block,
					tx_time=datetime(2026, 9, 7, 12, 0),
					status="success",
				)
			await engine.dispose()

			first = (await client.get("/v1/user/history", params={"limit": 2})).json()
			assert [r["txid"] for r in first["history"]] == ["tx-3", "tx-2"]
			assert first["next_cursor"] is not None

			second = (
				await client.get(
					"/v1/user/history",
					params={"limit": 2, "cursor": first["next_cursor"]},
				)
			).json()
			assert [r["txid"] for r in second["history"]] == ["tx-1"]
			assert second["next_cursor"] is None

			bad = await client.get("/v1/user/history", params={"cursor": "bogus"})
			assert bad.json()["error"]["code"] == "invalid_cursor"
		finally:
			await client.aclose()

	asyncio.run(scenario())


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
