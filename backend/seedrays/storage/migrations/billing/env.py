"""Alembic environment for the billing schema stream."""

from seedrays.storage.migrations.environment import run
from seedrays.storage.schema_billing import metadata

run(metadata)
