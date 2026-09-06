"""Finality state and the refined idempotency key (ADR-0017, ADR-0021).

Transactions gain the event index (several transfers of one asset inside
one transaction) and the finalization marker (provisional rows may vanish
on a chain reorganization). The idempotency key now includes the direction
and the event index. Bindings gain the per-network uniqueness of the
derivation index.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("transactions") as batch:
		batch.add_column(
			sa.Column("event_index", sa.Integer, nullable=False, server_default="0")
		)
		batch.add_column(sa.Column("finalized_at", sa.DateTime))
		batch.drop_constraint("uq_transactions_key", type_="unique")
		batch.create_unique_constraint(
			"uq_transactions_key",
			["txid", "address", "asset_id", "direction", "event_index"],
		)
	with op.batch_alter_table("bindings") as batch:
		batch.create_unique_constraint(
			"uq_bindings_index", ["wallet_id", "network", "derivation_index"]
		)


def downgrade() -> None:
	with op.batch_alter_table("bindings") as batch:
		batch.drop_constraint("uq_bindings_index", type_="unique")
	with op.batch_alter_table("transactions") as batch:
		batch.drop_constraint("uq_transactions_key", type_="unique")
		batch.create_unique_constraint(
			"uq_transactions_key", ["txid", "address", "asset_id"]
		)
		batch.drop_column("finalized_at")
		batch.drop_column("event_index")
