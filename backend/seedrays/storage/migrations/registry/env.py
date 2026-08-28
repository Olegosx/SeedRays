"""Alembic environment for the registry schema stream."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from seedrays.storage.schema_registry import metadata

config = context.config
target_metadata = metadata


def run_migrations_offline() -> None:
	"""Run migrations without a live database connection (SQL script mode)."""
	context.configure(
		url=config.get_main_option("sqlalchemy.url"),
		target_metadata=target_metadata,
		literal_binds=True,
	)
	with context.begin_transaction():
		context.run_migrations()


def run_migrations_online() -> None:
	"""Run migrations against a live database connection."""
	connectable = engine_from_config(
		config.get_section(config.config_ini_section, {}),
		prefix="sqlalchemy.",
		poolclass=pool.NullPool,
	)
	with connectable.connect() as connection:
		context.configure(connection=connection, target_metadata=target_metadata)
		with context.begin_transaction():
			context.run_migrations()


if context.is_offline_mode():
	run_migrations_offline()
else:
	run_migrations_online()
