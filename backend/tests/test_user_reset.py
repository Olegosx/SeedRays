"""Password reset routes: request by email, one-time token confirmation."""

import asyncio
import re
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seeding import FakeMailer, confirm_link, signed_in_client
from seedrays.storage.migrations.runner import upgrade_registry


def _reset_token(mailer: FakeMailer) -> str:
	"""The reset token from the last captured message."""
	text = mailer.messages[-1][2]
	match = re.search(r"/password-new\.html\?token=([\w~-]+)", text)
	assert match, f"no reset link in: {text!r}"
	return match.group(1)


def test_reset_flow_and_token_is_single_use(tmp_path: Path) -> None:
	"""The link sets a new password once, kills sessions, and dies after use."""

	async def scenario() -> None:
		mailer = FakeMailer()
		client, _csrf = await signed_in_client(tmp_path, mailer)
		try:
			assert (await client.get("/v1/user/me")).status_code == 200

			requested = await client.post(
				"/v1/user/password-reset", json={"email": "A@Example.com"}
			)
			assert requested.status_code == 200, requested.text
			token = _reset_token(mailer)
			assert mailer.messages[-1][0] == "a@example.com"

			weak = await client.post(
				"/v1/user/password-reset/confirm",
				json={"token": token, "new_password": "short"},
			)
			assert weak.json()["error"]["code"] == "weak_password"

			done = await client.post(
				"/v1/user/password-reset/confirm",
				json={"token": token, "new_password": "brand-new-pass"},
			)
			assert done.status_code == 200

			# Все сессии погашены; старый пароль мёртв, новый работает.
			assert (await client.get("/v1/user/me")).status_code == 401
			old = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse"},
			)
			assert old.status_code == 401
			fresh = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "brand-new-pass"},
			)
			assert fresh.status_code == 200

			# Токен одноразовый: повторное использование отбито.
			again = await client.post(
				"/v1/user/password-reset/confirm",
				json={"token": token, "new_password": "another-pass-1"},
			)
			assert again.json()["error"]["code"] == "invalid_token"
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_reset_does_not_reveal_addresses(tmp_path: Path) -> None:
	"""Unknown and unconfirmed emails answer ok and send nothing."""

	async def scenario() -> None:
		mailer = FakeMailer()
		client, csrf = await signed_in_client(tmp_path, mailer)
		try:
			sent_before = len(mailer.messages)

			unknown = await client.post(
				"/v1/user/password-reset", json={"email": "nobody@example.com"}
			)
			assert unknown.status_code == 200
			assert len(mailer.messages) == sent_before

			# Добавленная, но не подтверждённая почта — писать на неё нельзя.
			await client.post(
				"/v1/user/emails",
				json={"address": "second@example.com"},
				headers={"X-CSRF-Token": csrf},
			)
			sent_after_add = len(mailer.messages)
			unconfirmed = await client.post(
				"/v1/user/password-reset", json={"email": "second@example.com"}
			)
			assert unconfirmed.status_code == 200
			assert len(mailer.messages) == sent_after_add

			# Подтверждённая вторая почта — сброс работает и через неё.
			await client.get(confirm_link(mailer))
			confirmed = await client.post(
				"/v1/user/password-reset", json={"email": "second@example.com"}
			)
			assert confirmed.status_code == 200
			assert len(mailer.messages) == sent_after_add + 1
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_reset_requires_configured_mail(tmp_path: Path) -> None:
	"""Without outgoing mail the reset refuses even in the development mode."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		transport = httpx.ASGITransport(app=create_app(tmp_path, mailer=None))
		client = httpx.AsyncClient(transport=transport, base_url="https://gw")
		try:
			refused = await client.post(
				"/v1/user/password-reset", json={"email": "a@example.com"}
			)
			assert refused.status_code == 503
			assert refused.json()["error"]["code"] == "mail_not_configured"
		finally:
			await client.aclose()

	asyncio.run(scenario())
