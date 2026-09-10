"""Billing database schema (see ADR-0027).

The gateway owner's own financial data: what the users owe for the fee and
what they paid. A third database next to the registry and the per-user ones —
the registry holds no financial data (ADR-0008), and a user's database is
moved, restored and archived together with the user (ADR-0024), so the
owner's income cannot live there either.

Amounts follow the storage convention: TEXT holding an integer in minimal
units. Turnover, thresholds and invoice amounts are in micro-USDT (six
decimals) — the gateway's accounting unit for the fee. References to the
registry asset catalog and to users are plain integers: cross-database
foreign keys do not exist, integrity is kept at the application level.
"""

from sqlalchemy import (
	CheckConstraint,
	Column,
	DateTime,
	ForeignKey,
	Integer,
	MetaData,
	String,
	Table,
	Text,
	UniqueConstraint,
	func,
)

metadata = MetaData()

# Наблюдающие кошельки владельца шлюза, по одному на сеть оплаты: из них
# выводятся постоянные адреса счетов. Секрета здесь нет, как и у кошельков
# пользователей (ADR-0002).
master_wallets = Table(
	"master_wallets",
	metadata,
	Column("network", String(32), primary_key=True),
	Column("xpub", Text, nullable=False),
	# Отпечаток ключа: по нему идёт перекрёстная проверка «один xpub — один
	# кошелёк» с реестровым индексом пользовательских ключей, и он же не даёт
	# внести один и тот же кошелёк в две сети.
	Column("xpub_hash", String(128), nullable=False, unique=True),
	Column("added_at", DateTime, nullable=False, server_default=func.now()),
)

# Постоянный адрес оплаты: по одному на пару «пользователь + сеть», а не на
# счёт (ADR-0027). Индекс деривации уникален в сети — адрес выдаётся один раз.
invoice_addresses = Table(
	"invoice_addresses",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("user_id", Integer, nullable=False),
	Column("network", String(32), nullable=False),
	Column("address", String(128), nullable=False),
	Column("derivation_index", Integer, nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
	UniqueConstraint("user_id", "network", name="uq_invoice_addresses_owner"),
	UniqueConstraint("network", "address", name="uq_invoice_addresses_address"),
	UniqueConstraint("network", "derivation_index", name="uq_invoice_addresses_index"),
)

# Счёт за период. Ставка, порог и срок — снимок настроек на момент выставления:
# позднейшее изменение настроек не переписывает выставленные счета.
# Ручное подтверждение оператором живёт здесь же (кто и почему), а не строкой
# платежа: у подтверждения нет ни транзакции, ни актива, и синтетический
# платёж только запутал бы журнал наблюдений.
invoices = Table(
	"invoices",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("user_id", Integer, nullable=False),
	Column("period_start", DateTime, nullable=False),
	Column("period_end", DateTime, nullable=False),
	Column("turnover", Text, nullable=False),
	Column("rate_percent", Text, nullable=False),
	Column("threshold", Text, nullable=False),
	Column("amount", Text, nullable=False),
	Column("due_at", DateTime, nullable=False),
	Column("network", String(32), nullable=False),
	Column("address", String(128), nullable=False),
	Column("state", String(16), nullable=False, server_default="issued"),
	Column("credited", Text, nullable=False, server_default="0"),
	Column("issued_at", DateTime, nullable=False, server_default=func.now()),
	Column("paid_at", DateTime),
	Column("manual_operator_id", Integer),
	Column("manual_reason", Text, nullable=False, server_default=""),
	# Один счёт на пару «пользователь + период» — идемпотентность выставления
	# держится ключом схемы, а не аккуратностью задачи биллинга.
	UniqueConstraint("user_id", "period_start", name="uq_invoices_period"),
	CheckConstraint(
		"state IN ('issued', 'paid', 'overdue')", name="ck_invoices_state"
	),
)

# Поступления, наблюдаемые на адресах счетов. Ключ идемпотентности — тот же по
# смыслу, что у транзакций пользователя (ADR-0021). Строка без invoice_id ни к
# какому счёту не отнесена: так выглядит чужой актив на адресе счёта и
# переплата, ожидающая следующего счёта.
invoice_payments = Table(
	"invoice_payments",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("network", String(32), nullable=False),
	Column("address", String(128), nullable=False),
	Column("txid", String(128), nullable=False),
	Column("asset_id", Integer, nullable=False),
	Column("event_index", Integer, nullable=False, server_default="0"),
	Column("amount", Text, nullable=False),
	Column("tx_time", DateTime),
	Column("first_seen_at", DateTime, nullable=False, server_default=func.now()),
	Column("finalized_at", DateTime),
	Column("invoice_id", Integer, ForeignKey("invoices.id")),
	Column("credited", Text, nullable=False, server_default="0"),
	UniqueConstraint(
		"txid", "address", "asset_id", "event_index", name="uq_invoice_payments_key"
	),
)

# Состояние биллинга пользователя: чем платит и открыт ли доступ. Независимо от
# статуса учётной записи в реестре — административная блокировка и приостановка
# за неуплату снимаются каждая своим действием (ADR-0027).
# Пустая сеть означает «выбор по умолчанию»: конкретный код сети живёт в коде,
# а не в схеме, иначе умолчание пришлось бы менять миграцией.
user_billing = Table(
	"user_billing",
	metadata,
	Column("user_id", Integer, primary_key=True),
	Column("network", String(32), nullable=False, server_default=""),
	Column("state", String(16), nullable=False, server_default="ok"),
	Column("suspended_at", DateTime),
	CheckConstraint("state IN ('ok', 'suspended')", name="ck_user_billing_state"),
)
