"""Supervisor lifecycle tests: restarts on failure, stops on the stop event."""

import asyncio

from seedrays.orchestrator import supervisor


def test_supervised_restarts_after_crash_and_clean_exit(monkeypatch) -> None:
	"""Both a crash and an unexpected clean exit lead to a restart."""

	async def scenario() -> None:
		monkeypatch.setattr(supervisor, "RESTART_DELAY_SECONDS", 0.01)
		attempts = {"n": 0}
		forever = asyncio.Event()

		async def factory() -> None:
			attempts["n"] += 1
			if attempts["n"] == 1:
				raise RuntimeError("boom")  # сбой → перезапуск
			if attempts["n"] == 2:
				return  # неожиданный чистый выход → тоже перезапуск
			await forever.wait()  # третья попытка работает «вечно»

		stopping = asyncio.Event()
		task = asyncio.create_task(supervisor._supervised("unit", factory, stopping))
		while attempts["n"] < 3:
			await asyncio.sleep(0.01)
		task.cancel()
		try:
			await task
		except asyncio.CancelledError:
			pass
		assert attempts["n"] == 3

	asyncio.run(scenario())


def test_supervised_does_not_restart_when_stopping(monkeypatch) -> None:
	"""A clean exit during the gateway stop is final — no restart, no warning loop."""

	async def scenario() -> None:
		monkeypatch.setattr(supervisor, "RESTART_DELAY_SECONDS", 0.01)
		attempts = {"n": 0}
		stopping = asyncio.Event()

		async def factory() -> None:
			attempts["n"] += 1
			stopping.set()  # остановка пришла, пока компонент работал
			return  # компонент завершился штатно

		await asyncio.wait_for(
			supervisor._supervised("unit", factory, stopping), timeout=1.0
		)
		assert attempts["n"] == 1

	asyncio.run(scenario())


def test_trusted_proxies_setting(tmp_path) -> None:
	"""gateway.trusted_proxies is read from the registry; blank falls back."""

	async def scenario() -> None:
		from seedrays.storage import registry as registry_ops
		from seedrays.storage.engine import create_sqlite_engine, registry_db_path
		from seedrays.storage.migrations.runner import upgrade_registry

		upgrade_registry(tmp_path)
		assert await supervisor.resolve_trusted_proxies(tmp_path) == "127.0.0.1"

		registry = create_sqlite_engine(registry_db_path(tmp_path))
		try:
			await registry_ops.set_setting(
				registry, "gateway.trusted_proxies", " 10.0.0.5, 173.245.48.0/20 "
			)
		finally:
			await registry.dispose()
		assert (
			await supervisor.resolve_trusted_proxies(tmp_path)
			== "10.0.0.5, 173.245.48.0/20"
		)

	asyncio.run(scenario())
