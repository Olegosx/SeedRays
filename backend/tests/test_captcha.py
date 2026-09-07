"""Proof-of-work captcha: challenge issue, verification, one-time use."""

import asyncio
import base64
import json
import time
from pathlib import Path

import altcha

from seedrays.orchestrator.captcha import CaptchaGuard
from seeding import captcha_solution, signed_in_client


def _solve(challenge_dict: dict) -> str:
	"""Solve one issued challenge and encode the widget-style payload."""
	challenge = altcha.Challenge.from_dict(challenge_dict)
	solution = altcha.solve_challenge(challenge)
	assert solution is not None
	payload = {"challenge": challenge.to_dict(), "solution": solution.to_dict()}
	return base64.b64encode(json.dumps(payload).encode()).decode()


def test_valid_solution_is_accepted_exactly_once() -> None:
	"""A genuine solution passes; its replay is rejected."""
	guard = CaptchaGuard(cost=10)
	payload = _solve(guard.issue())
	assert guard.verify(payload) is True
	assert guard.verify(payload) is False  # одноразовость


def test_garbage_and_foreign_signature_are_rejected() -> None:
	"""Broken payloads and challenges signed by another process fail."""
	guard = CaptchaGuard(cost=10)
	assert guard.verify("not-base64-at-all") is False
	assert guard.verify(base64.b64encode(b'{"nope": 1}').decode()) is False
	# Задача, подписанная другим экземпляром (другой секрет), не принимается.
	foreign = CaptchaGuard(cost=10)
	assert guard.verify(_solve(foreign.issue())) is False


def test_expired_challenge_is_rejected() -> None:
	"""A solution of an expired challenge fails the check."""
	guard = CaptchaGuard(cost=10, ttl_seconds=-1)
	assert guard.verify(_solve(guard.issue())) is False


def test_tampered_challenge_is_rejected() -> None:
	"""Raising one's own expiry date breaks the signature."""
	guard = CaptchaGuard(cost=10, ttl_seconds=-1)
	challenge = guard.issue()
	challenge["parameters"]["expiresAt"] = int(time.time()) + 3600
	assert guard.verify(_solve(challenge)) is False


def test_login_routes_demand_the_captcha(tmp_path: Path) -> None:
	"""An unsolved captcha stops sign-in before any credential check."""

	async def scenario() -> None:
		client, _csrf = await signed_in_client(tmp_path)
		try:
			refused = await client.post(
				"/v1/user/login",
				json={
					"identifier": "alice",
					"password": "correct-horse",
					"captcha": "garbage",
				},
			)
			assert refused.status_code == 400
			assert refused.json()["error"]["code"] == "captcha_failed"

			# Решение одноразовое и на уровне маршрута.
			payload = await captcha_solution(client)
			first = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse", "captcha": payload},
			)
			assert first.status_code == 200, first.text
			replayed = await client.post(
				"/v1/user/login",
				json={"identifier": "alice", "password": "correct-horse", "captcha": payload},
			)
			assert replayed.status_code == 400
			assert replayed.json()["error"]["code"] == "captcha_failed"
		finally:
			await client.aclose()

	asyncio.run(scenario())
