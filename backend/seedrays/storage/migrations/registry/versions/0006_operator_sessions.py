"""Operator panel sessions (the operator route group of ADR-0004/0005).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"operator_sessions",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
		sa.Column(
			"operator_id", sa.Integer, sa.ForeignKey("operators.id"), nullable=False
		),
		sa.Column("csrf_token", sa.String(64), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("expires_at", sa.DateTime, nullable=False),
	)


def downgrade() -> None:
	op.drop_table("operator_sessions")
