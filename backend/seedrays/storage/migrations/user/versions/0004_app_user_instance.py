"""Application instances: the namespace of application user ids (ADR-0025).

Independent installations of one application share an API key, so their own
user ids collide: "user 42" of one installation and "user 42" of another are
different people, and the gateway used to hand them the same address. The
identity of an application user becomes the triple "application + instance +
external id".

A single-instance application is the empty instance, so existing rows need no
data migration: the new constraint is strictly weaker than the old one.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("app_users") as batch:
		batch.add_column(
			sa.Column("instance", sa.String(64), nullable=False, server_default="")
		)
		batch.drop_constraint("uq_app_users_app_external", type_="unique")
		batch.create_unique_constraint(
			"uq_app_users_identity", ["application_id", "instance", "external_id"]
		)


def downgrade() -> None:
	# Обратный шаг возможен только пока экземпляр один: если разные установки
	# успели завести пользователей с одинаковым внешним идентификатором,
	# восстановление прежнего ограничения упадёт — и это правильно, иначе
	# пришлось бы молча выбросить чьи-то привязки.
	with op.batch_alter_table("app_users") as batch:
		batch.drop_constraint("uq_app_users_identity", type_="unique")
		batch.create_unique_constraint(
			"uq_app_users_app_external", ["application_id", "external_id"]
		)
		batch.drop_column("instance")
