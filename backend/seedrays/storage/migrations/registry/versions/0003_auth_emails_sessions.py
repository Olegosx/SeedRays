"""User emails and cabinet sessions (user cabinet sign-in scenario).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"user_emails",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
		sa.Column("address", sa.String(255), nullable=False, unique=True),
		sa.Column("is_primary", sa.Integer, nullable=False, server_default="0"),
		sa.Column("confirmed_at", sa.DateTime),
		sa.Column("confirm_token_hash", sa.String(128)),
		sa.Column("confirm_expires_at", sa.DateTime),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"sessions",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
		sa.Column("csrf_token", sa.String(64), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("expires_at", sa.DateTime, nullable=False),
	)


def downgrade() -> None:
	op.drop_table("sessions")
	op.drop_table("user_emails")
