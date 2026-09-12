"""Alembic environment for the registry schema stream."""

from seedrays.storage.migrations.environment import run
from seedrays.storage.schema_registry import metadata

run(metadata)
