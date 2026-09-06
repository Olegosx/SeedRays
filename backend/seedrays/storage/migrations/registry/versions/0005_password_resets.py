"""Password reset tokens (the email-based recovery of the sign-in scenario).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"password_resets",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column(
			"user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, unique=True
		),
		sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("expires_at", sa.DateTime, nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)


def downgrade() -> None:
	op.drop_table("password_resets")
