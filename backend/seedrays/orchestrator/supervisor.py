"""The orchestrator supervisor: API server + watcher loop in one process.

One backend process (ADR-0003): the supervisor launches both activities as
parallel tasks, logs a failure of either and restarts it without taking
down the other.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn

from seedrays.api.app_api import create_app
from seedrays.watcher.loop import run_forever

logger = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 5.0


async def _supervised(name: str, factory: Callable[[], Awaitable[None]]) -> None:
	"""Run one activity forever: a crash or a clean exit both mean a restart."""
	while True:
		try:
			await factory()
			logger.warning("%s exited unexpectedly, restarting", name)
		except asyncio.CancelledError:
			raise
		except Exception:
			logger.exception("%s crashed, restarting", name)
		await asyncio.sleep(RESTART_DELAY_SECONDS)


async def run(
	data_dir: Path, host: str, port: int, frontend_dir: Path | None = None
) -> None:
	"""Run the gateway: the API server (with the static frontend) and the watcher.

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
	server = uvicorn.Server(config)
	logger.info("gateway starting: api on %s:%d, data dir %s", host, port, data_dir)
	await asyncio.gather(
		_supervised("api-server", server.serve),
		_supervised("watcher", lambda: run_forever(data_dir)),
	)
