"""Settings routes: secondary emails and password change."""

import asyncio
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seeding import FakeMailer, confirm_link, enable_dev_mail, signed_in_client
from seedrays.storage.migrations.runner import upgrade_registry


def test_secondary_email_lifecycle(tmp_path: Path) -> None:
	"""Add a second email, confirm it by the link, remove it; primary is protected."""

	async def scenario() -> None:
		mailer = FakeMailer()
		client, csrf = await signed_in_client(tmp_path, mailer)
		headers = {"X-CSRF-Token": csrf}
		try:
			added = await client.post(
				"/v1/user/emails", json={"address": "Backup@Example.com"}, headers=headers
			)
			assert added.status_code == 200, added.text
			assert added.json()["confirmation_required"] is True
			assert mailer.messages[-1][0] == "backup@example.com"

			me = (await client.get("/v1/user/me")).json()["user"]
			backup = next(e for e in me["emails"] if e["address"] == "backup@example.com")
			assert backup["primary"] is False
			assert backup["confirmed"] is False

			await client.get(confirm_link(mailer))
			me = (await client.get("/v1/user/me")).json()["user"]
			backup = next(e for e in me["emails"] if e["address"] == "backup@example.com")
			assert backup["confirmed"] is True

			dup = await client.post(
				"/v1/user/emails", json={"address": "backup@example.com"}, headers=headers
			)
			assert dup.json()["error"]["code"] == "email_taken"

			primary = next(e for e in me["emails"] if e["primary"])
			protected = await client.delete(
				f"/v1/user/emails/{primary['id']}", headers=headers
			)
			assert protected.json()["error"]["code"] == "cannot_remove_primary"

			removed = await client.delete(
				f"/v1/user/emails/{backup['id']}", headers=headers
			)
			assert removed.status_code == 200
			me = (await client.get("/v1/user/me")).json()["user"]
			assert [e["address"] for e in me["emails"]] == ["a@example.com"]
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_change_password_drops_other_sessions(tmp_path: Path) -> None:
	"""The old password stops working; the second session dies, the current stays."""

	async def scenario() -> None:
		upgrade_registry(tmp_path)
		await enable_dev_mail(tmp_path)
		app = create_app(tmp_path, mailer=None)
		first = httpx.AsyncClient(
			transport=httpx.ASGITransport(app=app), base_url="https://gw"
		)
		second = httpx.AsyncClient(
			transport=httpx.ASGITransport(app=app), base_url="https://gw"
		)
		try:
			await first.post(
				"/v1/user/register",
				json={
					"username": "alice",
					"email": "a@example.com",
					"password": "correct-horse",
				},
			)
			login = await first.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse"},
			)
			csrf = login.json()["csrf"]
			await second.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse"},
			)
			assert (await second.get("/v1/user/me")).status_code == 200

			wrong = await first.post(
				"/v1/user/password",
				json={"current_password": "nope-nope", "new_password": "new-password-1"},
				headers={"X-CSRF-Token": csrf},
			)
			assert wrong.json()["error"]["code"] == "invalid_credentials"

			changed = await first.post(
				"/v1/user/password",
				json={
					"current_password": "correct-horse",
					"new_password": "new-password-1",
				},
				headers={"X-CSRF-Token": csrf},
			)
			assert changed.status_code == 200

			# Текущая сессия жива, вторая — убита.
			assert (await first.get("/v1/user/me")).status_code == 200
			assert (await second.get("/v1/user/me")).status_code == 401

			old = await second.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse"},
			)
			assert old.status_code == 401
			fresh = await second.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "new-password-1"},
			)
			assert fresh.status_code == 200
		finally:
			await first.aclose()
			await second.aclose()

	asyncio.run(scenario())
