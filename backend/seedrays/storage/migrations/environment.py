"""Shared Alembic environment for all migration streams.

У потоков (реестр, база пользователя, биллинг) среда одинакова и
различается только набором метаданных, поэтому запуск живёт здесь, а
каждому потоку остаётся собственный ``env.py`` в три строки — файл,
который Alembic ищет по своей конвенции.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import MetaData, engine_from_config, pool


def run(target_metadata: MetaData) -> None:
	"""Run the migrations of one stream in the mode Alembic was asked for.

	Args:
		target_metadata: Schema metadata of the stream being migrated.
	"""
	config = context.config
	if context.is_offline_mode():
		context.configure(
			url=config.get_main_option("sqlalchemy.url"),
			target_metadata=target_metadata,
			literal_binds=True,
		)
		with context.begin_transaction():
			context.run_migrations()
		return
	connectable = engine_from_config(
		config.get_section(config.config_ini_section, {}),
		prefix="sqlalchemy.",
		poolclass=pool.NullPool,
	)
	with connectable.connect() as connection:
		context.configure(connection=connection, target_metadata=target_metadata)
		with context.begin_transaction():
			context.run_migrations()
