"""Indexes for the hot reads of the history and of the watcher pass.

The user database had no index at all, so every one of these reads scanned
the whole transactions table. Measured on 50 000 operations of one user: a
history page of five rows took 217 ms and materialized all 50 000 rows,
while the watcher's two per-pass queries scanned the table for every owner
in every network — even with nothing to apply and nothing to clean up.

The gateway is one process (ADR-0003): all of that time the watcher is not
scanning and billing is not checking payments. The cost grew with the
accumulated history rather than with the load, so it would have arrived on
its own, without any change in traffic.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
	# Порядок столбцов повторяет порядок чтений: сначала то, чем отбирают,
	# затем то, чем сортируют.
	op.create_index(
		"ix_transactions_incoming",
		"transactions",
		["direction", sa.text("block_number DESC"), sa.text("id DESC")],
	)
	op.create_index(
		"ix_transactions_unapplied",
		"transactions",
		["balance_applied_at", "finalized_at", "asset_id"],
	)
	op.create_index(
		"ix_transactions_provisional",
		"transactions",
		["finalized_at", "balance_applied_at", "block_number"],
	)


def downgrade() -> None:
	op.drop_index("ix_transactions_provisional", table_name="transactions")
	op.drop_index("ix_transactions_unapplied", table_name="transactions")
	op.drop_index("ix_transactions_incoming", table_name="transactions")
