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
	# Докуда адрес уже проверен у провайдера: следующий опрос идёт от этой
	# отметки с перекрытием, а не с начала истории адреса.
	Column("checked_at", DateTime),
	UniqueConstraint("user_id", "network", name="uq_invoice_addresses_owner"),
	UniqueConstraint("network", "address", name="uq_invoice_addresses_address"),
	UniqueConstraint("network", "derivation_index", name="uq_invoice_addresses_index"),
)

# Счёт за период — чистое начисление, минусовая сторона баланса пользователя.
# Ставка, порог и срок — снимок настроек на момент выставления: позднейшее
# изменение настроек не переписывает выставленные счета. Состояния и зачтённой
# суммы у счёта НЕТ: оплаченность выводится из баланса (сумма зачислений минус
# сумма счетов) — храним факты, состояние вычисляем, как подтверждения в
# ADR-0010.
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
	Column("issued_at", DateTime, nullable=False, server_default=func.now()),
	# Один счёт на пару «пользователь + период» — идемпотентность выставления
	# держится ключом схемы, а не аккуратностью задачи биллинга.
	UniqueConstraint("user_id", "period_start", name="uq_invoices_period"),
)

# Поступления, наблюдаемые на адресах счетов, — плюсовая сторона баланса.
# Ключ идемпотентности — тот же по смыслу, что у транзакций пользователя
# (ADR-0021). К счетам платежи не привязываются: баланс — это две суммы,
# а не распределение (решение владельца).
invoice_payments = Table(
	"invoice_payments",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("network", String(32), nullable=False),
	Column("address", String(128), nullable=False),
	Column("txid", String(128), nullable=False),
	Column("asset_id", Integer, nullable=False),
	Column("event_index", Integer, nullable=False, server_default="0"),
	# Сумма — в минимальных единицах актива, как пришла; оценка — она же в
	# микро-USDT, единице счёта. Ноль означает «деньгами счёта не является»:
	# так выглядит чужой токен, случайно присланный на адрес счёта.
	Column("amount", Text, nullable=False),
	Column("value", Text, nullable=False, server_default="0"),
	Column("tx_time", DateTime),
	Column("first_seen_at", DateTime, nullable=False, server_default=func.now()),
	Column("finalized_at", DateTime),
	# Владелец адреса: платёж зачисляется пользователю, а не счёту.
	Column("user_id", Integer, nullable=False),
	UniqueConstraint(
		"txid", "address", "asset_id", "event_index", name="uq_invoice_payments_key"
	),
)

# Ручные зачисления: деньги, пришедшие мимо шлюза и подтверждённые оператором.
# Отдельная таблица, а не синтетическая строка платежа: у зачисления нет ни
# транзакции, ни актива, и в журнале наблюдений ему не место.
manual_credits = Table(
	"manual_credits",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("user_id", Integer, nullable=False),
	Column("value", Text, nullable=False),
	Column("operator_id", Integer, nullable=False),
	Column("reason", Text, nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

# Отметки об отправленных уведомлениях — только для писем, которые иначе
# слались бы каждым проходом заново (напоминание о сроке, «выставить
# некуда»). Письма-переходы (счёт выставлен, доступ закрыт/открыт) отметок
# не требуют: сам переход случается один раз.
notices = Table(
	"notices",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("kind", String(32), nullable=False),
	# Предмет уведомления: «invoice:5», «user:3:2026-09» — вид определяет
	# трактовку. Одно письмо одного вида на предмет.
	Column("subject", String(64), nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
	UniqueConstraint("kind", "subject", name="uq_notices_key"),
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
