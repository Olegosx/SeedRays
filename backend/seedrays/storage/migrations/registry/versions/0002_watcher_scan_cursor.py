"""Add the scan cursor timestamp to watcher_state (ADR-0018).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column("watcher_state", sa.Column("last_scan_at", sa.DateTime))


def downgrade() -> None:
	op.drop_column("watcher_state", "last_scan_at")
