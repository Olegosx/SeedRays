"""Gateway-wide index of attached xpubs (one xpub — one wallet).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"wallet_xpubs",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("xpub_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)


def downgrade() -> None:
	op.drop_table("wallet_xpubs")
