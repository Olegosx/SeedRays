"""The continuous watcher mode: pass → pause → pass (see ADR-0003, ADR-0007).

Started and supervised by the orchestrator; a single pass is also available
as the ``watch`` console command.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from seedrays.storage.engine import create_sqlite_engine, registry_db_path
from seedrays.watcher.single_pass import PassStats, read_float_setting, run_pass

logger = logging.getLogger(__name__)

SETTING_INTERVAL = "watcher.interval_seconds"
DEFAULT_INTERVAL_SECONDS = 60.0


async def _interval(data_dir: Path) -> float:
	"""Read the pass interval from the registry settings; degrade to the default."""
	registry = create_sqlite_engine(registry_db_path(data_dir))
	try:
		return await read_float_setting(registry, SETTING_INTERVAL, DEFAULT_INTERVAL_SECONDS)
	finally:
		await registry.dispose()


async def run_forever(data_dir: Path) -> None:
	"""Run watcher passes until cancelled.

	A failed pass is logged and does not stop the loop; cancellation
	(asyncio.CancelledError) propagates to the supervisor.
	"""
	while True:
		try:
			stats: PassStats = await run_pass(data_dir)
			logger.info(
				"pass done: networks=%d matched=%d recorded=%d applied=%d deleted=%d rate_limited=%s",
				stats.networks_scanned,
				stats.transfers_matched,
				stats.rows_recorded,
				stats.rows_applied,
				stats.rows_deleted,
				",".join(stats.networks_rate_limited) or "-",
			)
		except asyncio.CancelledError:
			raise
		except Exception:
			logger.exception("watcher pass failed; continuing")
		try:
			interval = await _interval(data_dir)
		except asyncio.CancelledError:
			raise
		except Exception:
			# Недоступный реестр не должен ронять цикл: пауза по умолчанию,
			# следующая итерация попробует снова.
			logger.exception("failed to read the pass interval; using the default")
			interval = DEFAULT_INTERVAL_SECONDS
		await asyncio.sleep(interval)
