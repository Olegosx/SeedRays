"""Application key metadata: open prefix, issue time, revocable hash.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("applications") as batch:
		batch.add_column(
			sa.Column("key_prefix", sa.String(16), nullable=False, server_default="")
		)
		batch.add_column(sa.Column("key_issued_at", sa.DateTime))
		batch.alter_column("key_hash", existing_type=sa.String(128), nullable=True)


def downgrade() -> None:
	with op.batch_alter_table("applications") as batch:
		batch.alter_column("key_hash", existing_type=sa.String(128), nullable=False)
		batch.drop_column("key_issued_at")
		batch.drop_column("key_prefix")
