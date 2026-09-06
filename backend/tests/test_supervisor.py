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
