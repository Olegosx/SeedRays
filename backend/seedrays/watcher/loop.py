"""The continuous watcher mode: pass → pause → pass (see ADR-0003, ADR-0007).

Started and supervised by the orchestrator; a single pass is also available
as the ``watch`` console command.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import create_sqlite_engine, registry_db_path
from seedrays.watcher.single_pass import PassStats, run_pass

logger = logging.getLogger(__name__)

SETTING_INTERVAL = "watcher.interval_seconds"
DEFAULT_INTERVAL_SECONDS = 60.0


async def _interval(data_dir: Path) -> float:
	"""Read the pass interval from the registry settings."""
	registry = create_sqlite_engine(registry_db_path(data_dir))
	try:
		raw = await registry_ops.get_setting(registry, SETTING_INTERVAL)
	finally:
		await registry.dispose()
	return float(raw) if raw else DEFAULT_INTERVAL_SECONDS


async def run_forever(data_dir: Path) -> None:
	"""Run watcher passes until cancelled.

	A failed pass is logged and does not stop the loop; cancellation
	(asyncio.CancelledError) propagates to the supervisor.
	"""
	while True:
		try:
			stats: PassStats = await run_pass(data_dir)
			logger.info(
				"pass done: networks=%d matched=%d recorded=%d applied=%d rate_limited=%s",
				stats.networks_scanned,
				stats.transfers_matched,
				stats.rows_recorded,
				stats.rows_applied,
				",".join(stats.networks_rate_limited) or "-",
			)
		except asyncio.CancelledError:
			raise
		except Exception:
			logger.exception("watcher pass failed; continuing")
		await asyncio.sleep(await _interval(data_dir))
