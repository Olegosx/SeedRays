"""Initial per-user schema.

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
		"wallets",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("family", sa.String(16), nullable=False),
		sa.Column("xpub", sa.Text, nullable=False),
		sa.Column("label", sa.String(64), nullable=False, server_default=""),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"applications",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("name", sa.String(64), nullable=False),
		sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"app_networks",
		sa.Column(
			"application_id", sa.Integer, sa.ForeignKey("applications.id"), primary_key=True
		),
		sa.Column("network", sa.String(32), primary_key=True),
		sa.Column("wallet_id", sa.Integer, sa.ForeignKey("wallets.id"), nullable=False),
	)
	op.create_table(
		"app_users",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("application_id", sa.Integer, sa.ForeignKey("applications.id"), nullable=False),
		sa.Column("external_id", sa.String(255), nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.UniqueConstraint("application_id", "external_id", name="uq_app_users_app_external"),
		sa.UniqueConstraint("id", "application_id", name="uq_app_users_id_app"),
	)
	op.create_table(
		"bindings",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("wallet_id", sa.Integer, sa.ForeignKey("wallets.id"), nullable=False),
		sa.Column("network", sa.String(32), nullable=False),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("memo", sa.String(64), nullable=False, server_default=""),
		sa.Column("application_id", sa.Integer, nullable=False),
		sa.Column("app_user_id", sa.Integer, nullable=False),
		sa.Column("derivation_index", sa.Integer, nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.UniqueConstraint(
			"wallet_id", "network", "application_id", "app_user_id", name="uq_bindings_owner"
		),
		sa.UniqueConstraint("network", "address", "memo", name="uq_bindings_address"),
		sa.ForeignKeyConstraint(
			["app_user_id", "application_id"],
			["app_users.id", "app_users.application_id"],
			name="fk_bindings_app_user",
		),
	)
	op.create_table(
		"balances",
		sa.Column("address", sa.String(128), primary_key=True),
		sa.Column("asset_id", sa.Integer, primary_key=True),
		sa.Column("balance", sa.Text, nullable=False, server_default="0"),
		sa.Column("total_received", sa.Text, nullable=False, server_default="0"),
		sa.Column("last_deposit_at", sa.DateTime),
	)
	op.create_table(
		"transactions",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("txid", sa.String(128), nullable=False),
		sa.Column("asset_id", sa.Integer, nullable=False),
		sa.Column("direction", sa.String(3), nullable=False),
		sa.Column("amount", sa.Text, nullable=False),
		sa.Column("block_number", sa.Integer, nullable=False),
		sa.Column("tx_time", sa.DateTime),
		sa.Column("status", sa.String(8), nullable=False),
		sa.Column("first_seen_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("balance_applied_at", sa.DateTime),
		sa.UniqueConstraint("txid", "address", "asset_id", name="uq_transactions_key"),
		sa.CheckConstraint("direction IN ('in', 'out')", name="ck_transactions_direction"),
		sa.CheckConstraint("status IN ('success', 'failed')", name="ck_transactions_status"),
	)
	op.create_table(
		"mempool_queue",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("txid", sa.String(128), nullable=False),
		sa.Column("asset_id", sa.Integer, nullable=False),
		sa.Column("direction", sa.String(3), nullable=False),
		sa.Column("amount", sa.Text, nullable=False),
		sa.Column("first_seen_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("last_seen_at", sa.DateTime),
		sa.UniqueConstraint("txid", "address", "asset_id", name="uq_mempool_key"),
		sa.CheckConstraint("direction IN ('in', 'out')", name="ck_mempool_direction"),
	)


def downgrade() -> None:
	op.drop_table("mempool_queue")
	op.drop_table("transactions")
	op.drop_table("balances")
	op.drop_table("bindings")
	op.drop_table("app_users")
	op.drop_table("app_networks")
	op.drop_table("applications")
	op.drop_table("wallets")
