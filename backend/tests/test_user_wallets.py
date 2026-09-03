"""User wallet routes: list, attach with validation, one-time generation."""

import asyncio
from pathlib import Path

import httpx

from seedrays.api.app_api import create_app
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub, validate_mnemonic
from seedrays.storage.migrations.runner import upgrade_registry

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)


async def _signed_in_client(data_dir: Path) -> tuple[httpx.AsyncClient, str]:
	"""A client with a live session; returns (client, csrf token)."""
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


def test_attach_and_list_wallets(tmp_path: Path) -> None:
	"""A valid xpub attaches; a broken one is rejected; the list reflects it."""

	async def scenario() -> None:
		client, csrf = await _signed_in_client(tmp_path)
		try:
			empty = await client.get("/v1/user/wallets")
			assert empty.json()["wallets"] == []

			xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
			created = await client.post(
				"/v1/user/wallets",
				json={"family": "tron", "xpub": xpub, "label": "Main"},
				headers={"X-CSRF-Token": csrf},
			)
			assert created.status_code == 200, created.text
			assert created.json()["wallet"]["family"] == "tron"
			assert created.json()["wallet"]["addresses"] == 0

			bad = await client.post(
				"/v1/user/wallets",
				json={"family": "tron", "xpub": "xpub6NotAKey", "label": ""},
				headers={"X-CSRF-Token": csrf},
			)
			assert bad.status_code == 400
			assert bad.json()["error"]["code"] == "invalid_xpub"

			bad_family = await client.post(
				"/v1/user/wallets",
				json={"family": "doge", "xpub": xpub, "label": ""},
				headers={"X-CSRF-Token": csrf},
			)
			assert bad_family.json()["error"]["code"] == "invalid_family"

			# Без CSRF-заголовка изменяющий запрос отбивается.
			refused = await client.post(
				"/v1/user/wallets", json={"family": "tron", "xpub": xpub, "label": ""}
			)
			assert refused.status_code == 403

			listed = await client.get("/v1/user/wallets")
			assert [w["label"] for w in listed.json()["wallets"]] == ["Main"]
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_generate_returns_valid_material_and_stores_nothing(tmp_path: Path) -> None:
	"""Generation yields a valid phrase and matching xpubs without touching the DB."""

	async def scenario() -> None:
		client, csrf = await _signed_in_client(tmp_path)
		try:
			result = await client.post(
				"/v1/user/wallets/generate",
				json={"words": 12, "families": ["tron", "evm"]},
				headers={"X-CSRF-Token": csrf},
			)
			assert result.status_code == 200, result.text
			data = result.json()
			phrase = " ".join(data["phrase"])
			assert len(data["phrase"]) == 12
			assert validate_mnemonic(phrase)
			# xpub каждого семейства действительно выводится из этой фразы.
			by_family = {w["family"]: w["xpub"] for w in data["wallets"]}
			assert by_family["tron"] == account_xpub(phrase, Family.TRON)
			assert by_family["evm"] == account_xpub(phrase, Family.EVM)

			# Ничего не создано: список кошельков пуст.
			assert (await client.get("/v1/user/wallets")).json()["wallets"] == []

			bad_words = await client.post(
				"/v1/user/wallets/generate",
				json={"words": 15, "families": ["tron"]},
				headers={"X-CSRF-Token": csrf},
			)
			assert bad_words.json()["error"]["code"] == "invalid_words"

			# Кодовая фраза меняет xpub.
			with_pass = await client.post(
				"/v1/user/wallets/generate",
				json={"words": 12, "families": ["tron"], "passphrase": "x"},
				headers={"X-CSRF-Token": csrf},
			)
			passphrase_phrase = " ".join(with_pass.json()["phrase"])
			assert with_pass.json()["wallets"][0]["xpub"] == account_xpub(
				passphrase_phrase, Family.TRON, "x"
			)
		finally:
			await client.aclose()

	asyncio.run(scenario())
