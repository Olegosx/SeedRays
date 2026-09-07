"""Operator API: sign-in, gateway users, settings, watcher status."""

import asyncio
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.orchestrator.operator import create_operator
from seedrays.storage.engine import create_sqlite_engine, registry_db_path
from seedrays.storage.migrations.runner import upgrade_registry
from seeding import TEST_CAPTCHA_COST, captcha_solution, enable_dev_mail


async def _operator_login(
	client: httpx.AsyncClient, login: str, password: str
) -> httpx.Response:
	"""POST /operator/login with a freshly solved captcha."""
	return await client.post(
		"/v1/operator/login",
		json={
			"login": login,
			"password": password,
			"captcha": await captcha_solution(client, "/v1/operator/captcha"),
		},
	)


async def _user_login(
	client: httpx.AsyncClient, identifier: str, password: str
) -> httpx.Response:
	"""POST /user/login with a freshly solved captcha."""
	return await client.post(
		"/v1/user/login",
		json={
			"identifier": identifier,
			"password": password,
			"captcha": await captcha_solution(client),
		},
	)


async def _operator_client(data_dir: Path) -> tuple[httpx.AsyncClient, str]:
	"""A client signed in as a fresh operator; returns (client, csrf)."""
	upgrade_registry(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	await create_operator(registry, login="boss", password="operator-pass")
	await registry.dispose()
	transport = httpx.ASGITransport(
		app=create_app(data_dir, mailer=None, captcha_cost=TEST_CAPTCHA_COST)
	)
	client = httpx.AsyncClient(transport=transport, base_url="https://gw")
	login = await _operator_login(client, "boss", "operator-pass")
	assert login.status_code == 200, login.text
	return client, login.json()["csrf"]


def test_operator_sign_in_and_password_change(tmp_path: Path) -> None:
	"""Wrong credentials refused; a password change drops other panel sessions."""

	async def scenario() -> None:
		client, csrf = await _operator_client(tmp_path)
		try:
			wrong = await _operator_login(client, "boss", "nope-nope")
			assert wrong.status_code == 401
			assert (await client.get("/v1/operator/me")).json()["operator"]["login"] == "boss"

			# Без CSRF изменяющий запрос отбит.
			refused = await client.post(
				"/v1/operator/password",
				json={"current_password": "operator-pass", "new_password": "next-pass-1"},
			)
			assert refused.status_code == 403

			changed = await client.post(
				"/v1/operator/password",
				json={"current_password": "operator-pass", "new_password": "next-pass-1"},
				headers={"X-CSRF-Token": csrf},
			)
			assert changed.status_code == 200
			relogin = await _operator_login(client, "boss", "next-pass-1")
			assert relogin.status_code == 200
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_operator_manages_users(tmp_path: Path) -> None:
	"""The list shows users; blocking kills access; reset issues a one-time password."""

	async def scenario() -> None:
		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		await enable_dev_mail(tmp_path)
		user = httpx.AsyncClient(
			transport=httpx.ASGITransport(
				app=create_app(tmp_path, mailer=None, captcha_cost=TEST_CAPTCHA_COST)
			),
			base_url="https://gw",
		)
		try:
			await user.post(
				"/v1/user/register",
				json={
					"username": "alice",
					"email": "a@example.com",
					"password": "correct-horse",
					"captcha": await captcha_solution(user),
				},
			)
			await _user_login(user, "alice", "correct-horse")
			assert (await user.get("/v1/user/me")).status_code == 200

			listed = (await client.get("/v1/operator/users")).json()["users"]
			assert [u["username"] for u in listed] == ["alice"]
			assert listed[0]["status"] == "active"
			assert listed[0]["emails"][0]["address"] == "a@example.com"
			assert listed[0]["wallets"] == 0
			user_id = listed[0]["id"]

			# Блокировка: сессия пользователя умирает, вход закрыт.
			blocked = await client.post(
				f"/v1/operator/users/{user_id}/status",
				json={"status": "blocked"},
				headers=headers,
			)
			assert blocked.status_code == 200
			assert (await user.get("/v1/user/me")).status_code == 401
			refused = await _user_login(user, "alice", "correct-horse")
			assert refused.status_code == 401

			await client.post(
				f"/v1/operator/users/{user_id}/status",
				json={"status": "active"},
				headers=headers,
			)

			# Сброс пароля: временный пароль выдаётся один раз и работает.
			reset = await client.post(
				f"/v1/operator/users/{user_id}/password-reset", headers=headers
			)
			temp_password = reset.json()["password"]
			old = await _user_login(user, "alice", "correct-horse")
			assert old.status_code == 401
			fresh = await _user_login(user, "alice", temp_password)
			assert fresh.status_code == 200

			unknown = await client.post(
				"/v1/operator/users/999/password-reset", headers=headers
			)
			assert unknown.json()["error"]["code"] == "unknown_user"
		finally:
			await user.aclose()
			await client.aclose()

	asyncio.run(scenario())


def test_operator_settings_and_watcher(tmp_path: Path) -> None:
	"""Settings round-trip; secret values never come back; watcher block reads."""

	async def scenario() -> None:
		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			updated = await client.put(
				"/v1/operator/settings",
				json={
					"values": {
						"provider.trongrid.api_key": "super-secret-key",
						"watcher.interval_seconds": "30",
					}
				},
				headers=headers,
			)
			assert updated.status_code == 200, updated.text
			by_key = {s["key"]: s for s in updated.json()["settings"]}
			secret = by_key["provider.trongrid.api_key"]
			assert secret["set"] is True
			assert secret["value"] is None  # секрет наружу не возвращается
			assert by_key["watcher.interval_seconds"]["value"] == "30"

			# Пустой секрет в форме означает «не менять».
			kept = await client.put(
				"/v1/operator/settings",
				json={"values": {"provider.trongrid.api_key": ""}},
				headers=headers,
			)
			assert {s["key"]: s for s in kept.json()["settings"]}[
				"provider.trongrid.api_key"
			]["set"] is True

			bad = await client.put(
				"/v1/operator/settings",
				json={"values": {"nonsense.key": "1"}},
				headers=headers,
			)
			assert bad.json()["error"]["code"] == "unknown_setting"

			watcher = (await client.get("/v1/operator/watcher")).json()["networks"]
			assert {n["network"] for n in watcher} == {"tron", "tron-nile"}
		finally:
			await client.aclose()

	asyncio.run(scenario())
