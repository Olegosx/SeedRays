"""User API tests: registration, email confirmation, sessions, CSRF."""

import asyncio
import re
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.mail.base import MailSender
from seedrays.storage.migrations.runner import upgrade_registry

GOOD_USER = {"username": "alice", "email": "Alice@Example.com", "password": "correct-horse"}


class FakeMailer(MailSender):
	"""Captures outgoing messages instead of sending them."""

	def __init__(self) -> None:
		self.messages: list[tuple[str, str, str]] = []

	async def send(self, to: str, subject: str, text: str) -> None:
		self.messages.append((to, subject, text))


def _client(data_dir: Path, mailer: MailSender | None) -> httpx.AsyncClient:
	transport = httpx.ASGITransport(app=create_app(data_dir, mailer=mailer))
	return httpx.AsyncClient(transport=transport, base_url="http://gw")


def _confirm_link(mailer: FakeMailer) -> str:
	"""The confirmation path from the last captured message."""
	text = mailer.messages[-1][2]
	match = re.search(r"(/v1/user/confirm-email\?token=[\w~-]+)", text)
	assert match, f"no confirmation link in: {text!r}"
	return match.group(1)


def test_register_confirm_login_me_logout(tmp_path: Path) -> None:
	"""The full happy path with a configured mailer."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		mailer = FakeMailer()
		async with _client(tmp_path, mailer) as client:
			created = await client.post("/v1/user/register", json=GOOD_USER)
			assert created.status_code == 200
			assert created.json()["confirmation_required"] is True
			assert mailer.messages[0][0] == "alice@example.com"  # адрес приведён к нижнему регистру

			# До подтверждения почты вход закрыт.
			early = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": GOOD_USER["password"]},
			)
			assert early.status_code == 403
			assert early.json()["error"]["code"] == "email_not_confirmed"

			confirm = await client.get(_confirm_link(mailer))
			assert confirm.status_code == 303
			assert "confirmed=1" in confirm.headers["location"]

			# Вход по имени и по почте (регистр почты не важен).
			for identifier in ("alice", "ALICE@example.com"):
				login = await client.post(
					"/v1/user/login",
					json={"identifier": identifier, "password": GOOD_USER["password"]},
				)
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
		upgrade_registry(tmp_path)
		async with _client(tmp_path, None) as client:
			created = await client.post("/v1/user/register", json=GOOD_USER)
			assert created.status_code == 200
			assert created.json()["confirmation_required"] is False
			login = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": GOOD_USER["password"]},
			)
			assert login.status_code == 200

	asyncio.run(scenario())


def test_register_validation_and_duplicates(tmp_path: Path) -> None:
	"""Bad usernames/emails/passwords and duplicates get machine codes."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		async with _client(tmp_path, None) as client:
			cases = [
				({**GOOD_USER, "username": "a@b"}, "invalid_username"),
				({**GOOD_USER, "username": "ab"}, "invalid_username"),
				({**GOOD_USER, "email": "not-an-email"}, "invalid_email"),
				({**GOOD_USER, "password": "short"}, "weak_password"),
			]
			for body, code in cases:
				response = await client.post("/v1/user/register", json=body)
				assert response.json()["error"]["code"] == code, body

			assert (await client.post("/v1/user/register", json=GOOD_USER)).status_code == 200
			dup_name = await client.post(
				"/v1/user/register",
				json={**GOOD_USER, "email": "other@example.com"},
			)
			assert dup_name.status_code == 409
			assert dup_name.json()["error"]["code"] == "username_taken"
			dup_email = await client.post(
				"/v1/user/register", json={**GOOD_USER, "username": "bob"}
			)
			assert dup_email.status_code == 409
			assert dup_email.json()["error"]["code"] == "email_taken"

	asyncio.run(scenario())


def test_login_failures(tmp_path: Path) -> None:
	"""Wrong password and unknown identifier answer identically."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		async with _client(tmp_path, None) as client:
			await client.post("/v1/user/register", json=GOOD_USER)
			for identifier, password in (("alice", "wrong-password"), ("nobody", "whatever12")):
				response = await client.post(
					"/v1/user/login", json={"identifier": identifier, "password": password}
				)
				assert response.status_code == 401
				assert response.json()["error"]["code"] == "invalid_credentials"

	asyncio.run(scenario())


def test_bad_confirmation_token_redirects_with_zero(tmp_path: Path) -> None:
	"""An unknown token lands on the sign-in page with confirmed=0."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		async with _client(tmp_path, None) as client:
			response = await client.get("/v1/user/confirm-email?token=bogus")
			assert response.status_code == 303
			assert "confirmed=0" in response.headers["location"]

	asyncio.run(scenario())
