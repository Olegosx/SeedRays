"""Initial registry schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"users",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("login", sa.String(64), nullable=False, unique=True),
		sa.Column("password_hash", sa.Text, nullable=False),
		sa.Column("status", sa.String(16), nullable=False, server_default="active"),
		sa.Column("directory", sa.String(255), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"operators",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("login", sa.String(64), nullable=False, unique=True),
		sa.Column("password_hash", sa.Text, nullable=False),
		sa.Column("status", sa.String(16), nullable=False, server_default="active"),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"api_keys",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"settings",
		sa.Column("key", sa.String(64), primary_key=True),
		sa.Column("value", sa.Text, nullable=False),
	)
	op.create_table(
		"assets",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("network", sa.String(32), nullable=False),
		sa.Column("kind", sa.String(8), nullable=False),
		sa.Column("contract_address", sa.String(128), nullable=False, server_default=""),
		sa.Column("symbol", sa.String(32), nullable=False),
		sa.Column("decimals", sa.Integer, nullable=False),
		sa.Column("added_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.UniqueConstraint("network", "contract_address", name="uq_assets_network_contract"),
		sa.CheckConstraint("kind IN ('native', 'token')", name="ck_assets_kind"),
	)
	op.create_table(
		"watcher_state",
		sa.Column("network", sa.String(32), primary_key=True),
		sa.Column("last_block", sa.Integer, nullable=False),
		sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)


def downgrade() -> None:
	op.drop_table("watcher_state")
	op.drop_table("assets")
	op.drop_table("settings")
	op.drop_table("api_keys")
	op.drop_table("operators")
	op.drop_table("users")
