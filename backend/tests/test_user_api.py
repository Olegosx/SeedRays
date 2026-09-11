"""User API tests: registration, email confirmation, sessions, CSRF."""

import asyncio
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.mail.base import MailError, MailSender
from seeding import (
	TEST_CAPTCHA_COST,
	FakeMailer,
	captcha_solution,
	confirm_link,
	enable_dev_mail,
)
from seedrays.storage.migrations.runner import upgrade_all

GOOD_USER = {"username": "alice", "email": "Alice@Example.com", "password": "correct-horse"}


def _client(data_dir: Path, mailer: MailSender | None) -> httpx.AsyncClient:
	transport = httpx.ASGITransport(
		app=create_app(data_dir, mailer=mailer, captcha_cost=TEST_CAPTCHA_COST)
	)
	return httpx.AsyncClient(transport=transport, base_url="https://gw")


async def _register(client: httpx.AsyncClient, body: dict) -> httpx.Response:
	"""POST /register with a freshly solved captcha (solutions are one-time)."""
	return await client.post(
		"/v1/user/register", json={**body, "captcha": await captcha_solution(client)}
	)


async def _login(
	client: httpx.AsyncClient, identifier: str, password: str, **extra: object
) -> httpx.Response:
	"""POST /login with a freshly solved captcha (solutions are one-time)."""
	return await client.post(
		"/v1/user/login",
		json={
			"identifier": identifier,
			"password": password,
			"captcha": await captcha_solution(client),
			**extra,
		},
	)


def test_register_confirm_login_me_logout(tmp_path: Path) -> None:
	"""The full happy path with a configured mailer."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		mailer = FakeMailer()
		async with _client(tmp_path, mailer) as client:
			created = await _register(client, GOOD_USER)
			assert created.status_code == 200
			assert created.json()["confirmation_required"] is True
			assert mailer.messages[0][0] == "alice@example.com"  # адрес приведён к нижнему регистру

			# До подтверждения почты вход закрыт.
			early = await _login(client, "alice", GOOD_USER["password"])
			assert early.status_code == 403
			assert early.json()["error"]["code"] == "email_not_confirmed"

			confirm = await client.get(confirm_link(mailer))
			assert confirm.status_code == 303
			assert "confirmed=1" in confirm.headers["location"]

			# Вход по имени и по почте (регистр почты не важен).
			for identifier in ("alice", "ALICE@example.com"):
				login = await _login(client, identifier, GOOD_USER["password"])
				assert login.status_code == 200, login.text
			csrf = login.json()["csrf"]

			me = await client.get("/v1/user/me")
			assert me.status_code == 200
			assert me.json()["user"]["username"] == "alice"
			assert me.json()["user"]["emails"][0]["confirmed"] is True

			# Выход требует CSRF-токена.
			refused = await client.post("/v1/user/logout")
			assert refused.status_code == 403
			assert refused.json()["error"]["code"] == "csrf"
			out = await client.post("/v1/user/logout", headers={"X-CSRF-Token": csrf})
			assert out.status_code == 200
			assert (await client.get("/v1/user/me")).status_code == 401

	asyncio.run(scenario())


def test_register_without_mailer_autoconfirms(tmp_path: Path) -> None:
	"""Development mode: no mail sender — the email is confirmed immediately."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			created = await _register(client, GOOD_USER)
			assert created.status_code == 200
			assert created.json()["confirmation_required"] is False
			login = await _login(client, "alice", GOOD_USER["password"])
			assert login.status_code == 200

	asyncio.run(scenario())


def test_register_validation_and_duplicates(tmp_path: Path) -> None:
	"""Bad usernames/emails/passwords and duplicates get machine codes."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			cases = [
				({**GOOD_USER, "username": "a@b"}, "invalid_username"),
				({**GOOD_USER, "username": "ab"}, "invalid_username"),
				({**GOOD_USER, "email": "not-an-email"}, "invalid_email"),
				({**GOOD_USER, "password": "short"}, "weak_password"),
			]
			for body, code in cases:
				response = await _register(client, body)
				assert response.json()["error"]["code"] == code, body

			assert (await _register(client, GOOD_USER)).status_code == 200
			dup_name = await _register(client, {**GOOD_USER, "email": "other@example.com"})
			assert dup_name.status_code == 409
			assert dup_name.json()["error"]["code"] == "username_taken"
			dup_email = await _register(client, {**GOOD_USER, "username": "bob"})
			assert dup_email.status_code == 409
			assert dup_email.json()["error"]["code"] == "email_taken"

	asyncio.run(scenario())


def test_login_failures(tmp_path: Path) -> None:
	"""Wrong password and unknown identifier answer identically."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			await _register(client, GOOD_USER)
			for identifier, password in (("alice", "wrong-password"), ("nobody", "whatever12")):
				response = await _login(client, identifier, password)
				assert response.status_code == 401
				assert response.json()["error"]["code"] == "invalid_credentials"

	asyncio.run(scenario())


def test_bad_confirmation_token_redirects_with_zero(tmp_path: Path) -> None:
	"""An unknown token lands on the sign-in page with confirmed=0."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			response = await client.get("/v1/user/confirm-email?token=bogus")
			assert response.status_code == 303
			assert "confirmed=0" in response.headers["location"]

	asyncio.run(scenario())


def test_register_refused_without_mail_and_without_dev_mode(tmp_path: Path) -> None:
	"""No mail sender and no explicit dev mode — registration answers 503."""

	async def scenario() -> None:
		upgrade_all(tmp_path)  # флаг dev-почты сознательно НЕ включаем
		async with _client(tmp_path, None) as client:
			response = await _register(client, GOOD_USER)
			assert response.status_code == 503
			assert response.json()["error"]["code"] == "mail_not_configured"

	asyncio.run(scenario())


def test_login_rate_limited(tmp_path: Path) -> None:
	"""Password brute force hits the sliding-window limit with 429."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			await _register(client, GOOD_USER)
			for _ in range(10):
				attempt = await _login(client, "alice", "wrong-password")
				assert attempt.status_code == 401
			# Одиннадцатая попытка — даже с верным паролем — отбивается
			# лимитом ещё до проверки капчи (капча тут заведомо не решена).
			blocked = await client.post(
				"/v1/user/login",
				json={
					"identifier": "alice",
					"password": GOOD_USER["password"],
					"captcha": "irrelevant",
				},
			)
			assert blocked.status_code == 429
			assert blocked.json()["error"]["code"] == "rate_limited"

	asyncio.run(scenario())


def test_session_cookie_is_secure_and_httponly(tmp_path: Path) -> None:
	"""The session cookie never travels over plain HTTP and is JS-invisible."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			await _register(client, GOOD_USER)
			login = await _login(client, "alice", GOOD_USER["password"])
			cookie = login.headers["set-cookie"]
			assert "Secure" in cookie
			assert "HttpOnly" in cookie
			assert "SameSite=lax" in cookie

	asyncio.run(scenario())


def test_validation_error_does_not_echo_input(tmp_path: Path) -> None:
	"""The 400 body names the field and the reason, never the submitted value."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		async with _client(tmp_path, None) as client:
			secret = "very-secret-password-" + "x" * 1200  # длиннее лимита поля
			response = await client.post(
				"/v1/user/register",
				json={**GOOD_USER, "password": secret, "captcha": "stub"},
			)
			assert response.status_code == 400
			assert response.json()["error"]["code"] == "validation"
			assert "very-secret-password" not in response.text
			assert "password" in response.json()["error"]["message"]

	asyncio.run(scenario())


class BrokenMailer(MailSender):
	"""Always fails — models an outage of the mail provider."""

	async def send(self, to: str, subject: str, text: str) -> None:
		raise MailError("provider is down")


def test_failed_registration_mail_is_compensated(tmp_path: Path) -> None:
	"""A mail outage does not leave a dead half-registered account behind."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		async with _client(tmp_path, BrokenMailer()) as client:
			failed = await _register(client, GOOD_USER)
			assert failed.status_code == 502
			assert failed.json()["error"]["code"] == "mail_failed"

		# Повтор после починки почты: ни логин, ни адрес не заняты.
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			retried = await _register(client, GOOD_USER)
			assert retried.status_code == 200, retried.text

	asyncio.run(scenario())


def test_networks_and_families_come_from_backend(tmp_path: Path) -> None:
	"""The cabinet's pickers read networks/families from the API, not from markup."""

	async def scenario() -> None:
		upgrade_all(tmp_path)
		await enable_dev_mail(tmp_path)
		async with _client(tmp_path, None) as client:
			refused = await client.get("/v1/user/networks")
			assert refused.status_code == 401  # только для вошедших

			await _register(client, GOOD_USER)
			await _login(client, "alice", GOOD_USER["password"])
			data = (await client.get("/v1/user/networks")).json()
			assert {n["network"] for n in data["networks"]} == {"tron", "tron-nile"}
			assert all(n["family"] == "tron" for n in data["networks"])
			assert set(data["families"]) == {"tron", "evm"}
			# Шаблон ссылки на обозреватель блоков — с бэкенда, с местом под txid.
			assert all("{txid}" in n["explorer_tx"] for n in data["networks"])

	asyncio.run(scenario())
