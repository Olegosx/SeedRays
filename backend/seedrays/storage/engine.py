"""Database engine management: file layout, async engines, SQLite pragmas."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

REGISTRY_DB_FILENAME = "registry.db"
USER_DB_FILENAME = "user.db"
USERS_DIR_NAME = "users"


def registry_db_path(data_dir: Path) -> Path:
	"""Path of the shared registry database inside the gateway data directory."""
	return data_dir / REGISTRY_DB_FILENAME


def users_root(data_dir: Path) -> Path:
	"""Root directory holding one subdirectory per user."""
	return data_dir / USERS_DIR_NAME


def user_db_path(data_dir: Path, directory: str) -> Path:
	"""Path of a user's database given the user's directory name from the registry."""
	return users_root(data_dir) / directory / USER_DB_FILENAME


def sqlite_sync_url(path: Path) -> str:
	"""Synchronous SQLite URL; used by the migration runner (Alembic runs sync)."""
	return f"sqlite:///{path}"


def create_sqlite_engine(path: Path) -> AsyncEngine:
	"""Create an async SQLite engine with WAL mode and foreign keys enabled.

	Args:
		path: Database file location.

	Returns:
		The configured async engine.
	"""
	engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

	@event.listens_for(engine.sync_engine, "connect")
	def _configure_connection(dbapi_connection, _connection_record):
		# WAL: readers are not blocked by the writer.
		# foreign_keys: SQLite does not enforce FK constraints unless asked to.
		cursor = dbapi_connection.cursor()
		cursor.execute("PRAGMA journal_mode=WAL")
		cursor.execute("PRAGMA foreign_keys=ON")
		cursor.close()

	return engine
