"""Applying migrations: the registry database plus a loop over all user databases."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from seedrays.storage.engine import (
	registry_db_path,
	sqlite_sync_url,
	users_root,
	USER_DB_FILENAME,
)

_MIGRATIONS_DIR = Path(__file__).parent


def _make_config(stream: str, db_url: str) -> Config:
	"""Build an Alembic config for one migration stream and one database URL."""
	config = Config()
	config.set_main_option("script_location", str(_MIGRATIONS_DIR / stream))
	config.set_main_option("sqlalchemy.url", db_url)
	return config


def upgrade_registry(data_dir: Path) -> None:
	"""Migrate the shared registry database to the latest schema version."""
	data_dir.mkdir(parents=True, exist_ok=True)
	command.upgrade(_make_config("registry", sqlite_sync_url(registry_db_path(data_dir))), "head")


def upgrade_user_db(db_path: Path) -> None:
	"""Migrate one user database to the latest schema version."""
	command.upgrade(_make_config("user", sqlite_sync_url(db_path)), "head")


def upgrade_all(data_dir: Path) -> None:
	"""Migrate the registry and every existing user database.

	User databases are discovered on the filesystem (one ``user.db`` per
	subdirectory of the users root), so the loop works even before the
	registry is readable.
	"""
	upgrade_registry(data_dir)
	root = users_root(data_dir)
	if not root.exists():
		return
	for db_path in sorted(root.glob(f"*/{USER_DB_FILENAME}")):
		upgrade_user_db(db_path)
