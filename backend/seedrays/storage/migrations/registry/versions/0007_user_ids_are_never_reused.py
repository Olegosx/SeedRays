"""User ids must never be handed out twice (ADR-0024, ADR-0027).

One number identifies a person in all three databases: it is the ``user_id``
of the billing tables — the access state, the invoice address and the invoice
itself — and it is also the name of the directory holding the user's database.
SQLite, however, hands the number of the last deleted user to the next one who
registers, and that new person inherited a stranger's suspension, a stranger's
payment address and a stranger's debt. Paying the invoice shown to them would
settle somebody else's bill, to an address derived for somebody else.

Fixing the billing tables instead would not help: keeping the invoices out of
the user's own database is deliberate (ADR-0027), so the rows are meant to
outlive the account. What must not outlive it is the number pointing at them.

``sqlite_autoincrement`` keeps the high-water mark in ``sqlite_sequence``,
which deleting rows never lowers. Restoring a user from the archive still
inserts their previous id explicitly, as it always did.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
	# Пересоздание таблицы: единственный способ добавить AUTOINCREMENT в
	# SQLite. Дочерние таблицы ссылаются на users по значению, и при
	# переименовании ссылки сохраняются.
	with op.batch_alter_table(
		"users", table_kwargs={"sqlite_autoincrement": True}, recreate="always"
	):
		pass


def downgrade() -> None:
	with op.batch_alter_table("users", recreate="always"):
		pass
