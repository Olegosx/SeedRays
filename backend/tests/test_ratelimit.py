"""Sliding-window rate limiter unit tests."""

from seedrays.orchestrator.ratelimit import RateLimiter


def test_limit_and_window(monkeypatch) -> None:
	"""Attempts over the limit are refused until the window slides past them."""
	clock = {"now": 1000.0}
	monkeypatch.setattr("seedrays.orchestrator.ratelimit.time.monotonic", lambda: clock["now"])

	limiter = RateLimiter(limit=3, window_seconds=60.0)
	assert all(limiter.allow("k") for _ in range(3))
	assert limiter.allow("k") is False  # лимит исчерпан
	assert limiter.allow("other") is True  # другой ключ не задет

	clock["now"] += 61.0  # окно уехало — попытки снова разрешены
	assert limiter.allow("k") is True


def test_key_eviction(monkeypatch) -> None:
	"""The key map never grows past the cap under a unique-key flood."""
	clock = {"now": 1000.0}
	monkeypatch.setattr("seedrays.orchestrator.ratelimit.time.monotonic", lambda: clock["now"])

	limiter = RateLimiter(limit=1, window_seconds=60.0)
	for i in range(RateLimiter.MAX_KEYS + 100):
		limiter.allow(f"key-{i}")
	assert len(limiter._attempts) <= RateLimiter.MAX_KEYS
