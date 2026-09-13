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
	# tx-old — финализирована и применяется; tx-new — предварительная (pending).
	for txid, block, finalized in (("tx-old", 90, True), ("tx-new", 105, False)):
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
			finalized_at=datetime(2026, 8, 29, 12, 1) if finalized else None,
		)
	await user_store.apply_finalized(
		engine,
		asset_ids={asset.id},
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


def test_instances_are_separate_namespaces(tmp_path: Path) -> None:
	"""Independent installations sharing one key never share a payer (ADR-0025)."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			berlin = await client.post(
				"/v1/app/users/42/addresses",
				params={"instance": "berlin"},
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			assert berlin.status_code == 200, berlin.text
			berlin_address = berlin.json()["addresses"][0]["address"]
			# Первый выданный индекс кошелька — эталонный адрес 0.
			assert berlin_address == FIRST_TRON_ADDRESS

			# Тот же ключ и тот же идентификатор, но другая установка:
			# это другой плательщик, значит и адрес другой.
			munich = await client.post(
				"/v1/app/users/42/addresses",
				params={"instance": "munich"},
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			munich_address = munich.json()["addresses"][0]["address"]
			assert munich_address != berlin_address

			# Запрос без параметра — экземпляр по умолчанию, третье
			# пространство имён, а не «какое-нибудь из существующих».
			default = await client.post(
				"/v1/app/users/42/addresses",
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			default_address = default.json()["addresses"][0]["address"]
			assert default_address not in (berlin_address, munich_address)

			# Внутри своей установки выдача по-прежнему идемпотентна.
			repeat = await client.post(
				"/v1/app/users/42/addresses",
				params={"instance": "munich"},
				json={"networks": ["tron-nile"]},
				headers=HEADERS,
			)
			assert repeat.json()["addresses"][0]["address"] == munich_address

			# Чтение адресов ограничено своей установкой.
			read = await client.get(
				"/v1/app/users/42/addresses",
				params={"instance": "berlin"},
				headers=HEADERS,
			)
			assert [a["address"] for a in read.json()["addresses"]] == [berlin_address]

			# И список пользователей: в каждой установке ровно свой «42»,
			# а не все три сразу.
			for instance in ("berlin", "munich"):
				listed = await client.get(
					"/v1/app/users", params={"instance": instance}, headers=HEADERS
				)
				assert [u["external_id"] for u in listed.json()["users"]] == ["42"]
			default_list = await client.get("/v1/app/users", headers=HEADERS)
			assert [u["external_id"] for u in default_list.json()["users"]] == ["42"]

			# Слишком длинное имя экземпляра отбивается проверкой ввода
			# в едином формате ошибок, а не падает в базе.
			too_long = await client.get(
				"/v1/app/users", params={"instance": "x" * 65}, headers=HEADERS
			)
			assert too_long.status_code == 400
			assert too_long.json()["error"]["code"] == "validation"

	asyncio.run(scenario())


def test_instance_scopes_balances_and_history(tmp_path: Path) -> None:
	"""Money observed on one installation's address never surfaces in another."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			# Берлинский плательщик получает индекс 0 — эталонный адрес,
			# на который ниже кладутся тестовые поступления.
			for instance in ("berlin", "munich"):
				await client.post(
					"/v1/app/users/42/addresses",
					params={"instance": instance},
					json={"networks": ["tron-nile"]},
					headers=HEADERS,
				)
		await _seed_transactions(tmp_path)

		async with _client(tmp_path) as client:
			berlin = await client.get(
				"/v1/app/users/42/balances",
				params={"instance": "berlin"},
				headers=HEADERS,
			)
			rows = berlin.json()["balances"]
			assert len(rows) == 1
			assert rows[0]["total_received"] == "1000000"
			assert rows[0]["pending"] == "1000000"

			# У мюнхенского «42» свой адрес, на него ничего не приходило.
			munich = await client.get(
				"/v1/app/users/42/balances",
				params={"instance": "munich"},
				headers=HEADERS,
			)
			assert munich.json()["balances"] == []

			munich_history = await client.get(
				"/v1/app/users/42/history",
				params={"instance": "munich", "status": "all"},
				headers=HEADERS,
			)
			assert munich_history.json()["history"] == []

			berlin_history = await client.get(
				"/v1/app/users/42/history",
				params={"instance": "berlin", "status": "all"},
				headers=HEADERS,
			)
			assert len(berlin_history.json()["history"]) == 2

	asyncio.run(scenario())


def test_concurrent_address_issue_gets_distinct_addresses(tmp_path: Path) -> None:
	"""Concurrent issuance for different app users never shares an address."""

	async def scenario() -> None:
		await seed_gateway(tmp_path, NETWORKS)
		async with _client(tmp_path) as client:
			async def issue(user: str) -> httpx.Response:
				return await client.post(
					f"/v1/app/users/{user}/addresses",
					json={"networks": "all"},
					headers=HEADERS,
				)

			responses = await asyncio.gather(*(issue(f"cc-user{i}") for i in range(4)))
			assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
			# Один пользователь — один адрес (одинаковый в обеих сетях семейства);
			# у разных пользователей адреса не пересекаются.
			per_user = []
			for r in responses:
				addresses = {a["address"] for a in r.json()["addresses"]}
				assert len(addresses) == 1
				per_user.append(addresses.pop())
			assert len(set(per_user)) == len(per_user)

	asyncio.run(scenario())


def test_the_api_schema_is_not_published_by_default(tmp_path: Path) -> None:
	"""No anonymous visitor gets the route map of all three groups.

	Схема перечисляет маршруты всех трёх групп, включая операторские, вместе
	с формой тел запросов. Доступа это не даёт, но избавляет постороннего от
	необходимости что-либо угадывать.
	"""

	async def scenario() -> tuple[int, int, int]:
		await seed_gateway(tmp_path, NETWORKS)
		closed = create_app(tmp_path)
		async with httpx.AsyncClient(
			transport=httpx.ASGITransport(app=closed), base_url="http://gw"
		) as client:
			schema = await client.get("/openapi.json")
			docs = await client.get("/docs")
		opened = create_app(tmp_path, expose_schema=True)
		async with httpx.AsyncClient(
			transport=httpx.ASGITransport(app=opened), base_url="http://gw"
		) as client:
			on_request = await client.get("/openapi.json")
		return schema.status_code, docs.status_code, on_request.status_code

	schema, docs, on_request = asyncio.run(scenario())
	assert schema == 404, "схема отдаётся анонимно"
	assert docs == 404, "интерактивная документация отдаётся анонимно"
	assert on_request == 200, "по явному запросу схема должна открываться"
