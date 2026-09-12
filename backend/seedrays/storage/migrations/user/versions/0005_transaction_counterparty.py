"""The counterparty of an observed transfer (ADR-0027, turnover for the fee).

Moving funds between one's own addresses is not income and must stay out of
the turnover the gateway's fee is charged on. Recognizing such a move used to
rest on a guess — an outgoing row of the same transaction, asset and amount
existing somewhere in the database — because the row did not say who the
other side was. The guess is both too eager and too weak: an ordinary batch
payout trips it, and anyone able to put two equal transfers into one
transaction switches their whole turnover off.

Storing the counterparty replaces the guess with a fact: a row counts as an
internal move when the other side is a bound address of the same owner.

Rows written before this migration carry no counterparty and are counted as
income — the gateway has no way to learn their other side after the fact.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("transactions") as batch:
		batch.add_column(sa.Column("counterparty", sa.String(128)))


def downgrade() -> None:
	with op.batch_alter_table("transactions") as batch:
		batch.drop_column("counterparty")
