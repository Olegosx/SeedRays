"""In-memory sliding-window rate limiting for authentication endpoints.

The backend is a single process (ADR-0003), so process memory is the
honest storage for attempt counters: no extra infrastructure, counters
reset on restart — acceptable for a brute-force brake, not accounting.
"""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
	"""Sliding-window counter: at most ``limit`` attempts per ``window_seconds``.

	Every :meth:`allow` call counts as an attempt. Keys are caller-defined
	(client address, identifier, their combination).
	"""

	# Потолок числа ключей: защита памяти от перебора уникальных ключей.
	MAX_KEYS = 10_000

	def __init__(self, limit: int, window_seconds: float) -> None:
		"""Configure the limiter.

		Args:
			limit: Maximum attempts inside one window.
			window_seconds: Window length.
		"""
		self._limit = limit
		self._window = window_seconds
		self._attempts: dict[str, deque[float]] = {}

	def allow(self, key: str) -> bool:
		"""Register one attempt; False when the key is over its limit."""
		now = time.monotonic()
		attempts = self._attempts.setdefault(key, deque())
		while attempts and now - attempts[0] > self._window:
			attempts.popleft()
		if len(attempts) >= self._limit:
			return False
		attempts.append(now)
		if len(self._attempts) > self.MAX_KEYS:
			self._evict(now)
		return True

	def _evict(self, now: float) -> None:
		"""Drop expired keys; if still over the cap — the oldest ones."""
		for key in [k for k, dq in self._attempts.items() if not dq or now - dq[-1] > self._window]:
			del self._attempts[key]
		while len(self._attempts) > self.MAX_KEYS:
			# dict хранит порядок вставки: первый ключ — самый старый.
			del self._attempts[next(iter(self._attempts))]
