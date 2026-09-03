"""Application management routes: create/reissue/revoke keys, mappings, users."""

import asyncio
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.storage.migrations.runner import upgrade_registry

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)


async def _signed_in_client(data_dir: Path) -> tuple[httpx.AsyncClient, str]:
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
	return client, login.json()["csrf"]


def test_application_lifecycle_and_key_bridge(tmp_path: Path) -> None:
	"""Create → map network → the key opens the Application API; revoke closes it."""

	async def scenario() -> None:
		client, csrf = await _signed_in_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			created = await client.post(
				"/v1/user/applications", json={"name": "Shop"}, headers=headers
			)
			assert created.status_code == 200, created.text
			key = created.json()["key"]
			app_id = created.json()["application"]["id"]
			assert key.startswith("srk_")
			assert created.json()["application"]["key"]["prefix"] == key[:9]

			# Кошелёк + соответствие сети — чтобы ключ мог выдавать адреса.
			xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
			wallet = await client.post(
				"/v1/user/wallets",
				json={"family": "tron", "xpub": xpub, "label": "Main"},
				headers=headers,
			)
			wallet_id = wallet.json()["wallet"]["id"]
			mapped = await client.put(
				f"/v1/user/applications/{app_id}/networks",
				json={"network": "tron-nile", "wallet_id": wallet_id},
				headers=headers,
			)
			assert mapped.status_code == 200

			# Мост: ключ из кабинета работает в API приложений.
			issued = await client.post(
				"/v1/app/users/user1/addresses",
				json={"networks": "all"},
				headers={"X-API-Key": key},
			)
			assert issued.status_code == 200, issued.text
			assert issued.json()["addresses"][0]["network"] == "tron-nile"

			detail = await client.get(f"/v1/user/applications/{app_id}")
			assert detail.json()["application"]["users"] == 1
			assert detail.json()["networks"][0]["wallet_label"] == "Main"
			assert detail.json()["app_users"][0]["addresses"] == 1

			# Перевыпуск: старый ключ умирает, новый работает.
			reissued = await client.post(
				f"/v1/user/applications/{app_id}/key", headers=headers
			)
			new_key = reissued.json()["key"]
			assert new_key != key
			old_refused = await client.get(
				"/v1/app/users", headers={"X-API-Key": key}
			)
			assert old_refused.status_code == 401
			new_ok = await client.get("/v1/app/users", headers={"X-API-Key": new_key})
			assert new_ok.status_code == 200

			# Отзыв: доступ закрыт, признак в данных.
			revoked = await client.delete(
				f"/v1/user/applications/{app_id}/key", headers=headers
			)
			assert revoked.json()["application"]["key"]["revoked"] is True
			closed = await client.get("/v1/app/users", headers={"X-API-Key": new_key})
			assert closed.status_code == 401

			# Удаление соответствия.
			removed = await client.delete(
				f"/v1/user/applications/{app_id}/networks/tron-nile", headers=headers
			)
			assert removed.status_code == 200
			assert (
				await client.get(f"/v1/user/applications/{app_id}")
			).json()["networks"] == []
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_application_errors(tmp_path: Path) -> None:
	"""Unknown ids, bad names and a missing wallet get machine codes."""

	async def scenario() -> None:
		client, csrf = await _signed_in_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			assert (
				await client.get("/v1/user/applications/999")
			).json()["error"]["code"] == "unknown_application"
			bad_name = await client.post(
				"/v1/user/applications", json={"name": "   "}, headers=headers
			)
			assert bad_name.json()["error"]["code"] == "invalid_name"

			created = await client.post(
				"/v1/user/applications", json={"name": "Shop"}, headers=headers
			)
			app_id = created.json()["application"]["id"]
			bad_wallet = await client.put(
				f"/v1/user/applications/{app_id}/networks",
				json={"network": "tron", "wallet_id": 42},
				headers=headers,
			)
			assert bad_wallet.json()["error"]["code"] == "wallet_missing"

			# Изменяющие запросы без CSRF отбиваются.
			refused = await client.post("/v1/user/applications", json={"name": "X"})
			assert refused.status_code == 403
		finally:
			await client.aclose()

	asyncio.run(scenario())
