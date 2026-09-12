"""Security journal: precise reasons inside, neutral answers outside (ADR-0023)."""

import asyncio
import gzip
import json
from pathlib import Path

from seedrays.orchestrator.seclog import (
	LOG_DIR,
	LOG_FILENAME,
	SecurityLog,
	_gzip_rotator,
)
from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import create_sqlite_engine, registry_db_path
from seeding import captcha_solution, signed_in_client
from test_operator_api import _operator_client, _operator_login


def _events(data_dir: Path) -> list[dict]:
	"""Parse every journal line of a test gateway."""
	path = data_dir / LOG_DIR / LOG_FILENAME
	if not path.exists():
		return []
	return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_gzip_rotator_compresses_and_removes(tmp_path: Path) -> None:
	"""The rotation hook leaves only the compressed archive behind."""
	source = tmp_path / "security.log.1"
	source.write_text('{"event":"login"}\n')
	dest = tmp_path / "security.log.1.gz"
	_gzip_rotator(str(source), str(dest))
	assert not source.exists()
	assert gzip.decompress(dest.read_bytes()) == b'{"event":"login"}\n'


def test_rotation_settings_are_applied(tmp_path: Path) -> None:
	"""seclog.rotate_mb / seclog.backups configure the handler; junk falls back."""

	async def scenario() -> None:
		from seedrays.storage.migrations.runner import upgrade_registry

		upgrade_registry(tmp_path)
		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			await registry_ops.set_setting(registry, "seclog.rotate_mb", "5")
			await registry_ops.set_setting(registry, "seclog.backups", "junk")
			log = SecurityLog(tmp_path)
			await log.event(registry, "login", actor="user", outcome="success")
			assert log._handler is not None
			assert log._handler.maxBytes == 5 * 1024 * 1024
			assert log._handler.backupCount == 10  # мусор → значение по умолчанию
			log.close()
		finally:
			await registry.dispose()

	asyncio.run(scenario())


def test_login_outcomes_are_precise(tmp_path: Path) -> None:
	"""wrong_password and unknown_identifier are distinct in the journal
	while the API answers both with the same invalid_credentials."""

	async def scenario() -> None:
		client, _csrf = await signed_in_client(tmp_path)
		try:
			async def login(identifier: str, password: str):
				return await client.post(
					"/v1/user/login",
					json={
						"identifier": identifier,
						"password": password,
						"captcha": await captcha_solution(client),
					},
				)

			wrong = await login("alice", "not-the-password")
			nobody = await login("nobody", "whatever-pass")
			assert wrong.json()["error"]["code"] == "invalid_credentials"
			assert nobody.json()["error"]["code"] == "invalid_credentials"

			logins = [e for e in _events(tmp_path) if e["event"] == "login"]
			outcomes = [e["outcome"] for e in logins]
			assert outcomes == ["success", "wrong_password", "unknown_identifier"]
			assert logins[1]["identifier"] == "alice"
			assert logins[1]["user_id"] == logins[0]["user_id"]
			assert logins[2]["identifier"] == "nobody"
			assert "user_id" not in logins[2]
			# Паролей в журнале нет ни в каком виде.
			raw = (tmp_path / LOG_DIR / LOG_FILENAME).read_text()
			assert "not-the-password" not in raw
			assert "correct-horse" not in raw
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_reset_request_outcome_is_precise(tmp_path: Path) -> None:
	"""An unknown reset email answers ok outside, unknown_email inside."""

	async def scenario() -> None:
		from seeding import FakeMailer

		client, _csrf = await signed_in_client(tmp_path, FakeMailer())
		try:
			answer = await client.post(
				"/v1/user/password-reset",
				json={
					"email": "nobody@example.com",
					"captcha": await captcha_solution(client),
				},
			)
			assert answer.status_code == 200  # наружу существование не раскрывается

			requests = [
				e for e in _events(tmp_path) if e["event"] == "password_reset_request"
			]
			assert requests[-1]["outcome"] == "unknown_email"
			assert requests[-1]["identifier"] == "nobody@example.com"
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_operator_actions_are_journaled(tmp_path: Path) -> None:
	"""Operator sign-in and administrative actions land in the journal."""

	async def scenario() -> None:
		client, csrf = await _operator_client(tmp_path)
		headers = {"X-CSRF-Token": csrf}
		try:
			wrong = await _operator_login(client, "boss", "bad-password")
			assert wrong.status_code == 401

			updated = await client.put(
				"/v1/operator/settings",
				json={"values": {"provider.trongrid.api_key": "super-secret-value"}},
				headers=headers,
			)
			assert updated.status_code == 200

			events = _events(tmp_path)
			operator_logins = [
				e for e in events if e["event"] == "login" and e["actor"] == "operator"
			]
			assert [e["outcome"] for e in operator_logins] == ["success", "wrong_password"]

			settings = [e for e in events if e["event"] == "settings_update"]
			assert settings[-1]["detail"]["keys"] == ["provider.trongrid.api_key"]
			# Значения настроек (секреты!) в журнал не попадают.
			raw = (tmp_path / LOG_DIR / LOG_FILENAME).read_text()
			assert "super-secret-value" not in raw
		finally:
			await client.aclose()

	asyncio.run(scenario())


def test_a_broken_password_hash_is_not_recorded_as_a_wrong_password(tmp_path: Path) -> None:
	"""An unusable stored hash gets its own outcome, not the user's blame.

	Под исходом «неверный пароль» повреждение хеша выглядит как
	забывчивость пользователя: оператор, разбирая жалобу «не могу войти»,
	настоящей причины не увидит нигде (ADR-0023).
	"""

	async def scenario() -> None:
		from sqlalchemy import update

		from seedrays.storage import schema_registry
		from seedrays.storage.engine import create_sqlite_engine, registry_db_path

		client, _csrf = await signed_in_client(tmp_path)
		try:
			registry = create_sqlite_engine(registry_db_path(tmp_path))
			async with registry.begin() as conn:
				await conn.execute(
					update(schema_registry.users).values(password_hash="not-a-hash")
				)
			await registry.dispose()

			refused = await client.post(
				"/v1/user/login",
				json={
					"identifier": "alice",
					"password": "whatever-pass",
					"captcha": await captcha_solution(client),
				},
			)
			assert refused.json()["error"]["code"] == "invalid_credentials", (
				"наружу по-прежнему нейтральный отказ"
			)
			logins = [e for e in _events(tmp_path) if e["event"] == "login"]
			assert logins[-1]["outcome"] == "broken_password_hash"
		finally:
			await client.aclose()

	asyncio.run(scenario())
