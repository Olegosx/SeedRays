"""Initial billing schema (ADR-0027).

Revision ID: 0001
Revises:
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"master_wallets",
		sa.Column("network", sa.String(32), primary_key=True),
		sa.Column("xpub", sa.Text, nullable=False),
		sa.Column("xpub_hash", sa.String(128), nullable=False, unique=True),
		sa.Column("added_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
	)
	op.create_table(
		"invoice_addresses",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("user_id", sa.Integer, nullable=False),
		sa.Column("network", sa.String(32), nullable=False),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("derivation_index", sa.Integer, nullable=False),
		sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("checked_at", sa.DateTime),
		sa.UniqueConstraint("user_id", "network", name="uq_invoice_addresses_owner"),
		sa.UniqueConstraint("network", "address", name="uq_invoice_addresses_address"),
		sa.UniqueConstraint("network", "derivation_index", name="uq_invoice_addresses_index"),
	)
	op.create_table(
		"invoices",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("user_id", sa.Integer, nullable=False),
		sa.Column("period_start", sa.DateTime, nullable=False),
		sa.Column("period_end", sa.DateTime, nullable=False),
		sa.Column("turnover", sa.Text, nullable=False),
		sa.Column("rate_percent", sa.Text, nullable=False),
		sa.Column("threshold", sa.Text, nullable=False),
		sa.Column("amount", sa.Text, nullable=False),
		sa.Column("due_at", sa.DateTime, nullable=False),
		sa.Column("network", sa.String(32), nullable=False),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("state", sa.String(16), nullable=False, server_default="issued"),
		sa.Column("credited", sa.Text, nullable=False, server_default="0"),
		sa.Column("issued_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("paid_at", sa.DateTime),
		sa.Column("manual_operator_id", sa.Integer),
		sa.Column("manual_reason", sa.Text, nullable=False, server_default=""),
		sa.UniqueConstraint("user_id", "period_start", name="uq_invoices_period"),
		sa.CheckConstraint("state IN ('issued', 'paid', 'overdue')", name="ck_invoices_state"),
	)
	op.create_table(
		"invoice_payments",
		sa.Column("id", sa.Integer, primary_key=True),
		sa.Column("network", sa.String(32), nullable=False),
		sa.Column("address", sa.String(128), nullable=False),
		sa.Column("txid", sa.String(128), nullable=False),
		sa.Column("asset_id", sa.Integer, nullable=False),
		sa.Column("event_index", sa.Integer, nullable=False, server_default="0"),
		sa.Column("amount", sa.Text, nullable=False),
		sa.Column("value", sa.Text, nullable=False, server_default="0"),
		sa.Column("tx_time", sa.DateTime),
		sa.Column("first_seen_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
		sa.Column("finalized_at", sa.DateTime),
		sa.Column("invoice_id", sa.Integer, sa.ForeignKey("invoices.id")),
		sa.Column("credited", sa.Text, nullable=False, server_default="0"),
		sa.UniqueConstraint(
			"txid", "address", "asset_id", "event_index", name="uq_invoice_payments_key"
		),
	)
	op.create_table(
		"user_billing",
		sa.Column("user_id", sa.Integer, primary_key=True),
		sa.Column("network", sa.String(32), nullable=False, server_default=""),
		sa.Column("state", sa.String(16), nullable=False, server_default="ok"),
		sa.Column("suspended_at", sa.DateTime),
		sa.CheckConstraint("state IN ('ok', 'suspended')", name="ck_user_billing_state"),
	)


def downgrade() -> None:
	op.drop_table("user_billing")
	op.drop_table("invoice_payments")
	op.drop_table("invoices")
	op.drop_table("invoice_addresses")
	op.drop_table("master_wallets")
