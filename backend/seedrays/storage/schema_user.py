"""Per-user database schema (see ADR-0005, ADR-0009, ADR-0010, ADR-0017).

All financial and mapping data of one user: wallets, applications and
their users, bindings, balances, on-chain transactions and the mempool
queue.

Amounts are stored as TEXT: an integer in the asset's minimal units.
64-bit database integers overflow on 18-decimals tokens; Python ints do
not. Asset references are plain integers pointing at the registry
database catalog — cross-database foreign keys do not exist, integrity
is kept at the application level.
"""

from sqlalchemy import (
	CheckConstraint,
	Column,
	DateTime,
	ForeignKey,
	ForeignKeyConstraint,
	Integer,
	MetaData,
	String,
	Table,
	Text,
	UniqueConstraint,
	func,
)

metadata = MetaData()

wallets = Table(
	"wallets",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("family", String(16), nullable=False),
	Column("xpub", Text, nullable=False),
	Column("label", String(64), nullable=False, server_default=""),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

applications = Table(
	"applications",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("name", String(64), nullable=False),
	Column("key_hash", String(128), nullable=False, unique=True),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

app_networks = Table(
	"app_networks",
	metadata,
	Column("application_id", Integer, ForeignKey("applications.id"), primary_key=True),
	Column("network", String(32), primary_key=True),
	Column("wallet_id", Integer, ForeignKey("wallets.id"), nullable=False),
)

app_users = Table(
	"app_users",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("application_id", Integer, ForeignKey("applications.id"), nullable=False),
	Column("external_id", String(255), nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
	UniqueConstraint("application_id", "external_id", name="uq_app_users_app_external"),
	# Опора для составного внешнего ключа из bindings (согласованность ссылок).
	UniqueConstraint("id", "application_id", name="uq_app_users_id_app"),
)

bindings = Table(
	"bindings",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("wallet_id", Integer, ForeignKey("wallets.id"), nullable=False),
	Column("network", String(32), nullable=False),
	Column("address", String(128), nullable=False),
	Column("memo", String(64), nullable=False, server_default=""),
	Column("application_id", Integer, nullable=False),
	Column("app_user_id", Integer, nullable=False),
	Column("derivation_index", Integer, nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
	UniqueConstraint(
		"wallet_id", "network", "application_id", "app_user_id", name="uq_bindings_owner"
	),
	UniqueConstraint("network", "address", "memo", name="uq_bindings_address"),
	# Составной внешний ключ: пользователь приложения обязан принадлежать
	# тому же приложению, на которое ссылается привязка (ADR-0009).
	ForeignKeyConstraint(
		["app_user_id", "application_id"],
		["app_users.id", "app_users.application_id"],
		name="fk_bindings_app_user",
	),
)

balances = Table(
	"balances",
	metadata,
	Column("address", String(128), primary_key=True),
	Column("asset_id", Integer, primary_key=True),
	Column("balance", Text, nullable=False, server_default="0"),
	Column("total_received", Text, nullable=False, server_default="0"),
	Column("last_deposit_at", DateTime),
)

# Транзакции в блокчейне (ADR-0017): номер блока обязателен, провал исполнения —
# атрибут, финальность вычисляется от границы сети и не хранится. Строки после
# вставки меняются один раз — простановкой отметки «учтена в балансе».
transactions = Table(
	"transactions",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("address", String(128), nullable=False),
	Column("txid", String(128), nullable=False),
	Column("asset_id", Integer, nullable=False),
	Column("direction", String(3), nullable=False),
	Column("amount", Text, nullable=False),
	Column("block_number", Integer, nullable=False),
	Column("tx_time", DateTime),
	Column("status", String(8), nullable=False),
	Column("first_seen_at", DateTime, nullable=False, server_default=func.now()),
	Column("balance_applied_at", DateTime),
	UniqueConstraint("txid", "address", "asset_id", name="uq_transactions_key"),
	CheckConstraint("direction IN ('in', 'out')", name="ck_transactions_direction"),
	CheckConstraint("status IN ('success', 'failed')", name="ck_transactions_status"),
)

# Наблюдения очереди (мемпула): без номера блока; наполняется только там, где
# источник данных сети видит очередь (ADR-0017).
mempool_queue = Table(
	"mempool_queue",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("address", String(128), nullable=False),
	Column("txid", String(128), nullable=False),
	Column("asset_id", Integer, nullable=False),
	Column("direction", String(3), nullable=False),
	Column("amount", Text, nullable=False),
	Column("first_seen_at", DateTime, nullable=False, server_default=func.now()),
	Column("last_seen_at", DateTime),
	UniqueConstraint("txid", "address", "asset_id", name="uq_mempool_key"),
	CheckConstraint("direction IN ('in', 'out')", name="ck_mempool_direction"),
)
