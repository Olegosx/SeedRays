"""Alembic environment for the user schema stream."""

from seedrays.storage.migrations.environment import run
from seedrays.storage.schema_user import metadata

run(metadata)
