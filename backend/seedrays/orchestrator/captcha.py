"""Invisible proof-of-work protection for the anonymous auth endpoints.

ALTCHA challenges (ADR-0022): the browser solves a PBKDF2 key-derivation
puzzle in the background and attaches the solution to the request; the
server verifies it by HMAC signatures. The signing secret is random per
process and the used-nonce registry lives in process memory — the same
honest single-process storage as the rate limiter (ADR-0003): nothing to
configure, challenges simply restart with the process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time

import altcha

logger = logging.getLogger(__name__)

ALGORITHM = "PBKDF2/SHA-256"
# Цена одной попытки деривации; браузеру нужны в среднем сотни попыток,
# так что это ручка стоимости одной попытки перебора (~0.5 с CPU).
DEFAULT_COST = 10_000
CHALLENGE_TTL_SECONDS = 10 * 60


class CaptchaGuard:
	"""Issues signed proof-of-work challenges and verifies each solution once."""

	# Потолок реестра использованных решений — защита памяти (ср. RateLimiter).
	MAX_USED = 10_000

	def __init__(
		self, cost: int = DEFAULT_COST, ttl_seconds: float = CHALLENGE_TTL_SECONDS
	) -> None:
		"""Configure the guard.

		Args:
			cost: PBKDF2 iteration count of one derivation attempt.
			ttl_seconds: Challenge lifetime; a solution older than this
				is rejected as expired.
		"""
		self._cost = cost
		self._ttl = ttl_seconds
		self._secret = secrets.token_hex(32)
		# nonce задачи → unix-время её истечения (для очистки реестра).
		self._used: dict[str, float] = {}

	def issue(self) -> dict:
		"""Create one signed challenge in the ALTCHA wire format."""
		challenge = altcha.create_challenge(
			ALGORITHM,
			self._cost,
			expires_at=int(time.time() + self._ttl),
			hmac_secret=self._secret,
			hmac_key_secret=self._secret,
		)
		return challenge.to_dict()

	async def verify(self, payload: str) -> bool:
		"""Check one solution; every solution is accepted at most once.

		Проверка доказательства работы считает PBKDF2 и занимает единицы
		миллисекунд — она уходит в отдельный поток. Процесс у шлюза один
		(ADR-0003), и на потоке входов эти миллисекунды складываются в
		простой watcher. Реестр использованных решений меняется только
		в цикле событий, поэтому гонок между потоками не возникает.

		Args:
			payload: Base64-encoded JSON with the challenge and its
				solution, as produced by the ALTCHA widget.

		Returns:
			True when the solution is genuine, unexpired and fresh.
		"""
		result = await asyncio.to_thread(
			altcha.verify_solution, payload, self._secret, hmac_key_secret=self._secret
		)
		if not result.verified:
			logger.info(
				"captcha solution rejected: expired=%s error=%s", result.expired, result.error
			)
			return False
		try:
			decoded = json.loads(base64.b64decode(payload).decode())
			parameters = decoded["challenge"]["parameters"]
			nonce = str(parameters["nonce"])
			expires_at = float(parameters.get("expiresAt") or 0)
		except (ValueError, KeyError, TypeError):
			# Верификатор выше принимает только парсящийся payload, так что
			# сюда попасть некуда, — но молча доверять внешним данным нельзя.
			logger.warning("captcha payload passed verification but failed parsing")
			return False
		if nonce in self._used:
			logger.info("captcha solution replayed; rejected")
			return False
		self._used[nonce] = expires_at
		if len(self._used) > self.MAX_USED:
			self._evict(time.time())
		return True

	def _evict(self, now: float) -> None:
		"""Drop expired nonces; if still over the cap — the oldest entries."""
		for nonce in [n for n, expires in self._used.items() if expires <= now]:
			del self._used[nonce]
		while len(self._used) > self.MAX_USED:
			# dict хранит порядок вставки: первый ключ — самый старый.
			del self._used[next(iter(self._used))]
