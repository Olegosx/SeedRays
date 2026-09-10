"""Shared test helpers: gateway seeding, the fake mailer, signed-in clients."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import altcha
import httpx
from sqlalchemy import insert

from seedrays.api.app_api import create_app
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub
from seedrays.mail.base import MailSender
from seedrays.orchestrator.operations import hash_api_key
from seedrays.storage import registry as registry_ops
from seedrays.storage import schema_registry, schema_user
from seedrays.storage.engine import create_sqlite_engine, registry_db_path, user_db_path
from seedrays.storage.migrations.runner import upgrade_all

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)
TEST_API_KEY = "test-api-key-0001"
# m/44'/195'/0'/0/0 этой фразы — эталонный адрес из тестов деривации.
FIRST_TRON_ADDRESS = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"

# Дешёвая сложность капчи, чтобы тесты решали задачи мгновенно.
TEST_CAPTCHA_COST = 10


async def captcha_solution(
	client: httpx.AsyncClient, path: str = "/v1/user/captcha"
) -> str:
	"""Fetch one proof-of-work challenge from the gateway and solve it."""
	response = await client.get(path)
	assert response.status_code == 200, response.text
	challenge = altcha.Challenge.from_dict(response.json())
	solution = altcha.solve_challenge(challenge)
	assert solution is not None, "the test captcha challenge was not solved"
	payload = {"challenge": challenge.to_dict(), "solution": solution.to_dict()}
	return base64.b64encode(json.dumps(payload).encode()).decode()


class FakeMailer(MailSender):
	"""Captures outgoing messages instead of sending them."""

	def __init__(self) -> None:
		self.messages: list[tuple[str, str, str]] = []

	async def send(self, to: str, subject: str, text: str) -> None:
		self.messages.append((to, subject, text))


def confirm_link(mailer: FakeMailer) -> str:
	"""The confirmation path from the last captured message."""
	text = mailer.messages[-1][2]
	match = re.search(r"(/v1/user/confirm-email\?token=[\w~-]+)", text)
	assert match, f"no confirmation link in: {text!r}"
	return match.group(1)


async def signed_in_client(
	data_dir: Path, mailer: MailSender | None = None
) -> tuple[httpx.AsyncClient, str]:
	"""A client with a live session of a fresh user; returns (client, csrf).

	Мигрирует реестр, включает dev-режим почты, регистрирует пользователя
	alice (с подтверждением по письму, если передан почтовик) и входит.
	"""
	upgrade_all(data_dir)
	await enable_dev_mail(data_dir)
	transport = httpx.ASGITransport(
		app=create_app(data_dir, mailer=mailer, captcha_cost=TEST_CAPTCHA_COST)
	)
	client = httpx.AsyncClient(transport=transport, base_url="https://gw")
	await client.post(
		"/v1/user/register",
		json={
			"username": "alice",
			"email": "a@example.com",
			"password": "correct-horse",
			"captcha": await captcha_solution(client),
		},
	)
	if mailer is not None:
		await client.get(confirm_link(mailer))
	login = await client.post(
		"/v1/user/login",
		json={
			"identifier": "alice",
			"password": "correct-horse",
			"captcha": await captcha_solution(client),
		},
	)
	return client, login.json()["csrf"]


async def enable_dev_mail(data_dir: Path) -> None:
	"""Turn on the explicit development auto-confirm mail mode for a test gateway."""
	registry = create_sqlite_engine(registry_db_path(data_dir))
	try:
		await registry_ops.set_setting(registry, "mail.dev_autoconfirm", "1")
	finally:
		await registry.dispose()


async def seed_gateway(data_dir: Path, networks: tuple[str, ...] = ("tron-nile",)) -> None:
	"""Create a migrated gateway with one user, wallet, application and API key.

	The wallet is the reference test wallet (TRON family), the application
	is mapped to the given networks and authenticated by TEST_API_KEY.
	"""
	upgrade_all(data_dir)
	registry = create_sqlite_engine(registry_db_path(data_dir))
	user = await registry_ops.create_user(registry, data_dir, "alice", "password-hash")

	key_hash = hash_api_key(TEST_API_KEY)
	async with registry.begin() as conn:
		await conn.execute(
			insert(schema_registry.api_keys).values(key_hash=key_hash, user_id=user.id)
		)
	await registry.dispose()

	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	engine = create_sqlite_engine(user_db_path(data_dir, user.directory))
	async with engine.begin() as conn:
		await conn.execute(insert(schema_user.wallets).values(family="tron", xpub=xpub))
		await conn.execute(
			insert(schema_user.applications).values(name="shop", key_hash=key_hash)
		)
		for network in networks:
			await conn.execute(
				insert(schema_user.app_networks).values(
					application_id=1, network=network, wallet_id=1
				)
			)
	await engine.dispose()
