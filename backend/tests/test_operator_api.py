"""Operator API: sign-in, gateway users, settings, watcher status."""

import asyncio
from datetime import datetime
import json
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.orchestrator.operations import OperationError
from seedrays.orchestrator.operator import create_operator, restore_user
from seedrays.storage.engine import archive_root, create_sqlite_engine, registry_db_path
from seedrays.storage.migrations.runner import upgrade_all
from seeding import (
	TEST_CAPTCHA_COST,
	captcha_solution,
	enable_dev_mail,
	signed_in_client,
)


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
	upgrade_all(data_dir)
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


def test_operator_deletes_and_restores_user(tmp_path: Path) -> None:
	"""Delete moves a blocked user into the archive; the CLI path restores them."""

	TEST_MNEMONIC = (
		"abandon abandon abandon abandon abandon abandon "
		"abandon abandon abandon abandon abandon about"
	)

	async def scenario() -> None:
		# alice с кошельком — чтобы проверить переезд базы и индекса xpub.
		user, user_csrf = await signed_in_client(tmp_path)
		xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
		attached = await user.post(
			"/v1/user/wallets",
			json={"family": "tron", "xpub": xpub, "label": "Main"},
			headers={"X-CSRF-Token": user_csrf},
		)
		assert attached.status_code == 200, attached.text

		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			listed = (await client.get("/v1/operator/users")).json()["users"]
			user_id = listed[0]["id"]

			# Активного удалить нельзя — сначала блокировка.
			active = await client.post(
				f"/v1/operator/users/{user_id}/delete",
				json={"username": "alice"},
				headers=headers,
			)
			assert active.status_code == 409
			assert active.json()["error"]["code"] == "user_not_blocked"

			await client.post(
				f"/v1/operator/users/{user_id}/status",
				json={"status": "blocked"},
				headers=headers,
			)

			# Логин сверяется и на сервере, не только в форме панели.
			mismatch = await client.post(
				f"/v1/operator/users/{user_id}/delete",
				json={"username": "alicia"},
				headers=headers,
			)
			assert mismatch.json()["error"]["code"] == "username_mismatch"

			deleted = await client.post(
				f"/v1/operator/users/{user_id}/delete",
				json={"username": "alice"},
				headers=headers,
			)
			assert deleted.status_code == 200, deleted.text
			assert (await client.get("/v1/operator/users")).json()["users"] == []

			# Архив: снимок реестра + каталог пользователя с базой.
			archives = list(archive_root(tmp_path).iterdir())
			assert len(archives) == 1
			bundle = json.loads((archives[0] / "registry.json").read_text())
			assert bundle["user"]["login"] == "alice"
			assert len(bundle["wallet_xpubs"]) == 1
			directory = bundle["user"]["directory"]
			assert (archives[0] / directory / "user.db").is_file()
			assert not (tmp_path / "users" / directory).exists()

			# Имя и xpub освободились: новая регистрация и привязка проходят.
			fresh, fresh_csrf = await signed_in_client(tmp_path)
			reattached = await fresh.post(
				"/v1/user/wallets",
				json={"family": "tron", "xpub": xpub, "label": "Again"},
				headers={"X-CSRF-Token": fresh_csrf},
			)
			assert reattached.status_code == 200, reattached.text

			# Пока имя занято новой alice — восстановление честно отказывает.
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			try:
				conflict = None
				try:
					await restore_user(registry, tmp_path, archive_dir=archives[0])
				except OperationError as exc:
					conflict = exc
				assert conflict is not None and conflict.code == "restore_conflict"

				# Убираем новую alice тем же путём удаления — и восстанавливаем старую.
				second = (await client.get("/v1/operator/users")).json()["users"][0]
				await client.post(
					f"/v1/operator/users/{second['id']}/status",
					json={"status": "blocked"},
					headers=headers,
				)
				await client.post(
					f"/v1/operator/users/{second['id']}/delete",
					json={"username": "alice"},
					headers=headers,
				)
				login = await restore_user(registry, tmp_path, archive_dir=archives[0])
				assert login == "alice"
			finally:
				await registry.dispose()
			await fresh.aclose()

			# Восстановленная alice: статус сохранён («заблокирован»), база на месте.
			restored = (await client.get("/v1/operator/users")).json()["users"]
			assert [u["username"] for u in restored] == ["alice"]
			assert restored[0]["status"] == "blocked"
			assert restored[0]["wallets"] == 1

			# После разблокировки старый пароль работает.
			await client.post(
				f"/v1/operator/users/{restored[0]['id']}/status",
				json={"status": "active"},
				headers=headers,
			)
			back = await _user_login(user, "alice", "correct-horse")
			assert back.status_code == 200, back.text
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

			# Пробел — то же «не менять»: значение нормализуется до проверки,
			# иначе поле из одних пробелов стирало бы сохранённый ключ.
			spaced = await client.put(
				"/v1/operator/settings",
				json={"values": {"provider.trongrid.api_key": "   "}},
				headers=headers,
			)
			assert {s["key"]: s for s in spaced.json()["settings"]}[
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


def test_numeric_settings_are_validated(tmp_path: Path) -> None:
	"""Numeric fields are checked before storage; blank clears them."""

	async def scenario() -> None:
		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			for key, value in (
				("watcher.interval_seconds", "часто"),
				("watcher.overlap_minutes", "-5"),
				("seclog.rotate_mb", "0"),
				("seclog.backups", "2.5"),  # целое поле не принимает дробь
				# Ставка живёт в сотых долях процента: точнее — значит счёт
				# считался бы по одной ставке, а печатал другую.
				("billing.rate_percent", "0.125"),
				("billing.rate_percent", "-1"),
			):
				refused = await client.put(
					"/v1/operator/settings",
					json={"values": {key: value}},
					headers=headers,
				)
				assert refused.status_code == 400, f"{key}={value!r}: {refused.text}"
				assert refused.json()["error"]["code"] == "invalid_setting"

			# Ни одно значение не записано: отказ целиком, а не половина формы.
			current = {
				s["key"]: s for s in (await client.get("/v1/operator/settings")).json()["settings"]
			}
			assert current["watcher.interval_seconds"]["value"] == ""
			assert current["seclog.rotate_mb"]["value"] == ""

			# Годные значения проходят; пустое поле — «снять настройку».
			ok = await client.put(
				"/v1/operator/settings",
				json={
					"values": {
						"watcher.interval_seconds": "45",
						"provider.trongrid.rate_per_sec": "2.5",
						"seclog.backups": "3",
						"watcher.overlap_minutes": "",
					}
				},
				headers=headers,
			)
			assert ok.status_code == 200, ok.text
			stored = {s["key"]: s for s in ok.json()["settings"]}
			assert stored["watcher.interval_seconds"]["value"] == "45"
			assert stored["provider.trongrid.rate_per_sec"]["value"] == "2.5"
			assert stored["seclog.backups"]["value"] == "3"
			assert stored["watcher.overlap_minutes"]["value"] == ""

			# Одно негодное поле отменяет всю отправку, включая годные соседние.
			mixed = await client.put(
				"/v1/operator/settings",
				json={"values": {"watcher.interval_seconds": "90", "seclog.backups": "нет"}},
				headers=headers,
			)
			assert mixed.status_code == 400
			after = {
				s["key"]: s for s in (await client.get("/v1/operator/settings")).json()["settings"]
			}
			assert after["watcher.interval_seconds"]["value"] == "45"
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_operator_manages_master_wallets(tmp_path: Path) -> None:
	"""The owner enters a master wallet per network and can take it back."""

	async def scenario() -> tuple[list, list, int, str]:
		from seedrays.families import Family
		from seedrays.keygen.generate import account_xpub

		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		xpub = account_xpub(
			"legal winner thank year wave sausage worth useful legal winner thank yellow",
			Family.TRON,
		)
		put = await client.put(
			"/v1/operator/billing/wallets",
			json={"network": "tron", "xpub": xpub},
			headers=headers,
		)
		assert put.status_code == 200, put.text
		listed = (await client.get("/v1/operator/billing/wallets")).json()
		# Приватный ключ отвергается так же, как у кошелька пользователя.
		private = await client.put(
			"/v1/operator/billing/wallets",
			json={"network": "tron-nile", "xpub": "xprvBADKEY"},
			headers=headers,
		)
		await client.delete("/v1/operator/billing/wallets/tron", headers=headers)
		empty = (await client.get("/v1/operator/billing/wallets")).json()
		await client.aclose()
		return listed["wallets"], empty["wallets"], private.status_code, listed["networks"][0]

	wallets, empty, private_status, first_network = asyncio.run(scenario())
	assert [w["network"] for w in wallets] == ["tron"]
	assert empty == []
	assert private_status == 400
	assert first_network in ("tron", "tron-nile")


def test_operator_sees_invoices_and_confirms_payment(tmp_path: Path) -> None:
	"""The panel lists invoices and settles one on the operator's word."""

	async def scenario() -> tuple[dict, int, dict, int]:
		import sys

		sys.path.insert(0, "tests")
		import test_billing as tb

		from seedrays.orchestrator import billing
		from seedrays.storage import billing as billing_store

		billing_engine, _address = await tb._gateway_with_invoice(tmp_path)
		try:
			await billing.run_pass(tmp_path, now=datetime(2026, 10, 20))
			client, csrf = await _operator_client(tmp_path)
			headers = {"X-CSRF-Token": csrf}
			listed = (await client.get("/v1/operator/billing/invoices?state=overdue")).json()
			invoice_id = listed["invoices"][0]["id"]
			# Без причины подтверждение не проходит.
			no_reason = await client.post(
				f"/v1/operator/billing/invoices/{invoice_id}/confirm",
				json={"reason": "   "},
				headers=headers,
			)
			confirmed = await client.post(
				f"/v1/operator/billing/invoices/{invoice_id}/confirm",
				json={"reason": "paid by bank transfer"},
				headers=headers,
			)
			assert confirmed.status_code == 200, confirmed.text
			after = (await client.get("/v1/operator/billing/invoices")).json()
			from seedrays.storage import registry as registry_ops

			registry = create_sqlite_engine(registry_db_path(tmp_path))
			user = await registry_ops.get_user_by_login(registry, "alice")
			await registry.dispose()
			state = await billing_store.access_state(billing_engine, user.id)
			await client.aclose()
			return listed["invoices"][0], no_reason.status_code, after["invoices"][0], state
		finally:
			await billing_engine.dispose()

	overdue, no_reason_status, settled, access = asyncio.run(scenario())
	assert overdue["state"] == "overdue"
	assert overdue["username"] == "alice"
	assert overdue["amount"] == "15"
	assert no_reason_status == 400, "причина обязательна"
	assert settled["state"] == "paid"
	assert settled["manual_reason"] == "paid by bank transfer"
	assert access == "ok", "ручное подтверждение возвращает доступ"


def test_billing_settings_are_validated(tmp_path: Path) -> None:
	"""The fee fields join the settings page and refuse junk before storing."""

	async def scenario() -> tuple[set, int, int]:
		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		keys = {s["key"] for s in (await client.get("/v1/operator/settings")).json()["settings"]}
		bad_list = await client.put(
			"/v1/operator/settings",
			json={"values": {"billing.assets.tron": "not json"}},
			headers=headers,
		)
		good = await client.put(
			"/v1/operator/settings",
			json={
				"values": {
					"billing.enabled": "1",
					"billing.rate_percent": "1.5",
					"billing.assets.tron": '["TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"]',
				}
			},
			headers=headers,
		)
		await client.aclose()
		return keys, bad_list.status_code, good.status_code

	keys, bad_status, good_status = asyncio.run(scenario())
	assert "billing.rate_percent" in keys
	assert "billing.assets.tron" in keys
	assert "billing.payment_assets.tron-nile" in keys
	assert bad_status == 400, "сломанный список контрактов отвергается целиком"
	assert good_status == 200
