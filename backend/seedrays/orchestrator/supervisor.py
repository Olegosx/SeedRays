"""The orchestrator supervisor: API server + watcher loop in one process.

One backend process (ADR-0003): the supervisor launches both activities as
parallel tasks and owns the process lifecycle. A component crash restarts
that component without taking down the other; a stop signal (SIGTERM /
SIGINT) shuts the whole gateway down gracefully — the API server finishes
its connections, the watcher task is cancelled between passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Awaitable, Callable, Generator

import uvicorn

from seedrays.api.app_api import create_app
from seedrays.watcher.loop import run_forever

logger = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 5.0


class _SupervisedServer(uvicorn.Server):
	"""Uvicorn server that does not capture process signals.

	The stock ``serve()`` installs its own SIGINT/SIGTERM handlers and
	returns "cleanly" on a stop signal — indistinguishable from a crash
	for the supervisor, which owns the process lifecycle here. Signals
	are handled by :func:`run`; the server is stopped by setting
	``should_exit``.
	"""

	@contextlib.contextmanager
	def capture_signals(self) -> Generator[None, None, None]:
		yield


async def _supervised(
	name: str, factory: Callable[[], Awaitable[None]], stopping: asyncio.Event
) -> None:
	"""Run one activity until the gateway stops; restart it on any failure.

	Args:
		name: Component name for the log.
		factory: Creates and runs one attempt of the activity; a fresh
			attempt is made after every failure.
		stopping: The gateway-wide stop event: set — no more restarts.
	"""
	while not stopping.is_set():
		try:
			await factory()
			if stopping.is_set():
				break
			logger.warning("%s exited unexpectedly, restarting", name)
		except asyncio.CancelledError:
			logger.info("%s stopped", name)
			raise
		except Exception:
			logger.exception("%s crashed, restarting", name)
		# Пауза перед перезапуском, прерываемая остановкой шлюза.
		with contextlib.suppress(asyncio.TimeoutError):
			await asyncio.wait_for(stopping.wait(), timeout=RESTART_DELAY_SECONDS)
	logger.info("%s stopped", name)


async def run(
	data_dir: Path, host: str, port: int, frontend_dir: Path | None = None
) -> None:
	"""Run the gateway until a stop signal: the API server and the watcher.

	Args:
		data_dir: The gateway data directory.
		host: API bind address.
		port: API bind port.
		frontend_dir: Static frontend directory; None — API only.
	"""
	config = uvicorn.Config(
		create_app(data_dir, frontend_dir=frontend_dir),
		host=host,
		port=port,
		log_level="info",
	)
	stopping = asyncio.Event()
	# Текущий экземпляр сервера: на каждый (пере)запуск создаётся новый —
	# отработавший uvicorn.Server повторно служить не может (should_exit
	# остаётся выставленным).
	current_server: _SupervisedServer | None = None

	async def api_factory() -> None:
		nonlocal current_server
		current_server = _SupervisedServer(config)
		await current_server.serve()

	loop = asyncio.get_running_loop()

	def request_stop(sig_name: str) -> None:
		logger.info("received %s, stopping the gateway", sig_name)
		stopping.set()

	handled = []
	for sig in (signal.SIGINT, signal.SIGTERM):
		try:
			loop.add_signal_handler(sig, request_stop, sig.name)
			handled.append(sig)
		except NotImplementedError:
			# Платформа без add_signal_handler (Windows): остановка —
			# KeyboardInterrupt, его обрабатывает вызывающая сторона (CLI).
			logger.warning("signal handling unavailable for %s on this platform", sig.name)

	api_task = asyncio.create_task(_supervised("api-server", api_factory, stopping))
	watcher_task = asyncio.create_task(
		_supervised("watcher", lambda: run_forever(data_dir), stopping)
	)
	logger.info("gateway starting: api on %s:%d, data dir %s", host, port, data_dir)
	try:
		await stopping.wait()
		# Плавная остановка: API дорабатывает открытые соединения,
		# watcher отменяется между проходами (проход идемпотентен).
		if current_server is not None:
			current_server.should_exit = True
		watcher_task.cancel()
		await asyncio.gather(api_task, watcher_task, return_exceptions=True)
		logger.info("gateway stopped")
	finally:
		for sig in handled:
			loop.remove_signal_handler(sig)
