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
	Column("updated_at", DateTime, nullable=False, server_default=func.now()),
)
