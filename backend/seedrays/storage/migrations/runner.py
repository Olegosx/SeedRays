"""Applying migrations: the registry database plus a loop over all user databases."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from seedrays.storage.engine import (
	registry_db_path,
	sqlite_sync_url,
	users_root,
	USER_DB_FILENAME,
)

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent


def _make_config(stream: str, db_url: str) -> Config:
	"""Build an Alembic config for one migration stream and one database URL."""
	config = Config()
	config.set_main_option("script_location", str(_MIGRATIONS_DIR / stream))
	config.set_main_option("sqlalchemy.url", db_url)
	return config


def upgrade_registry(data_dir: Path) -> None:
	"""Migrate the shared registry database to the latest schema version.

	Raises:
		RuntimeError: With the database path in the message when the
			migration fails — the caller's log must name the victim.
	"""
	data_dir.mkdir(parents=True, exist_ok=True)
	db_path = registry_db_path(data_dir)
	logger.info("migrating registry database %s", db_path)
	try:
		command.upgrade(_make_config("registry", sqlite_sync_url(db_path)), "head")
	except Exception as exc:
		raise RuntimeError(f"registry migration failed for {db_path}: {exc}") from exc


def upgrade_user_db(db_path: Path) -> None:
	"""Migrate one user database to the latest schema version.

	Raises:
		RuntimeError: With the database path in the message when the
			migration fails.
	"""
	logger.info("migrating user database %s", db_path)
	try:
		command.upgrade(_make_config("user", sqlite_sync_url(db_path)), "head")
	except Exception as exc:
		raise RuntimeError(f"user database migration failed for {db_path}: {exc}") from exc


def upgrade_all(data_dir: Path) -> None:
	"""Migrate the registry and every existing user database.

	User databases are discovered on the filesystem (one ``user.db`` per
	subdirectory of the users root), so the loop works even before the
	registry is readable.

	Stops at the first failure: continuing after a broken database would
	hide it behind later successes (the raised error names the file).
	"""
	upgrade_registry(data_dir)
	root = users_root(data_dir)
	migrated = 0
	if root.exists():
		for db_path in sorted(root.glob(f"*/{USER_DB_FILENAME}")):
			upgrade_user_db(db_path)
			migrated += 1
	logger.info("migrations done: registry + %d user database(s)", migrated)
