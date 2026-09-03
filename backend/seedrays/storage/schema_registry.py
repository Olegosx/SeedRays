"""Shared registry database schema (see ADR-0008, ADR-0010).

Holds no financial data: users, operators, the API-key index, gateway
settings, the asset catalog and the watcher service state.
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

users = Table(
	"users",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("login", String(64), nullable=False, unique=True),
	Column("password_hash", Text, nullable=False),
	Column("status", String(16), nullable=False, server_default="active"),
	Column("directory", String(255), nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

operators = Table(
	"operators",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("login", String(64), nullable=False, unique=True),
	Column("password_hash", Text, nullable=False),
	Column("status", String(16), nullable=False, server_default="active"),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

api_keys = Table(
	"api_keys",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("key_hash", String(128), nullable=False, unique=True),
	Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

settings = Table(
	"settings",
	metadata,
	Column("key", String(64), primary_key=True),
	Column("value", Text, nullable=False),
)

# Почтовые адреса пользователя: основная почта + добавленные (сценарий кабинета).
# Адрес хранится в нижнем регистре; подтверждение — по токену из письма
# (хранится отпечаток SHA-256, сам токен уходит только в письмо).
user_emails = Table(
	"user_emails",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
	Column("address", String(255), nullable=False, unique=True),
	Column("is_primary", Integer, nullable=False, server_default="0"),
	Column("confirmed_at", DateTime),
	Column("confirm_token_hash", String(128)),
	Column("confirm_expires_at", DateTime),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

# Сессии кабинета: кука несёт случайный токен, в базе — его отпечаток.
# csrf_token сверяется с заголовком X-CSRF-Token на изменяющих запросах.
sessions = Table(
	"sessions",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("token_hash", String(128), nullable=False, unique=True),
	Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
	Column("csrf_token", String(64), nullable=False),
	Column("created_at", DateTime, nullable=False, server_default=func.now()),
	Column("expires_at", DateTime, nullable=False),
)

assets = Table(
	"assets",
	metadata,
	Column("id", Integer, primary_key=True),
	Column("network", String(32), nullable=False),
	Column("kind", String(8), nullable=False),
	# Пустая строка вместо NULL у нативной монеты: иначе уникальность
	# (network, contract_address) не работает — NULL в SQL не равен NULL.
	Column("contract_address", String(128), nullable=False, server_default=""),
	Column("symbol", String(32), nullable=False),
	Column("decimals", Integer, nullable=False),
	Column("added_at", DateTime, nullable=False, server_default=func.now()),
	UniqueConstraint("network", "contract_address", name="uq_assets_network_contract"),
	CheckConstraint("kind IN ('native', 'token')", name="ck_assets_kind"),
)

watcher_state = Table(
	"watcher_state",
	metadata,
	Column("network", String(32), primary_key=True),
	Column("last_block", Integer, nullable=False),
	# Курсор сканирования: провайдеры фильтруют по времени, не по высоте (ADR-0018).
	Column("last_scan_at", DateTime),
	Column("updated_at", DateTime, nullable=False, server_default=func.now()),
)
