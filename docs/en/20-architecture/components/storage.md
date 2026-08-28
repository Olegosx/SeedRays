[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/storage.md)

# Storage Layer

The interface between the gateway logic and the database. Components above it never see SQL.

## Interface Level

The abstraction sits at the level of domain operations — "save an incoming payment", "list
the wallet's addresses", "mark an address as used" — not at the level of a universal SQL
pass-through. The concrete backend implements those operations in its own dialect.
See [ADR-0006](../decisions/0006-storage-abstraction.md). This mirrors the principle of the
[Chain Abstraction](chain-abstraction.md): components above the interface do not know what
is underneath.

## Per-User Storage

Each user has their own database and working directory
(see [ADR-0005](../decisions/0005-multi-user-model.md)):

- Isolation is structural: a query physically cannot reach another user's data.
- The user's lifecycle (backup, transfer, deletion) is an operation on one directory.
- Write contention in SQLite is spread across per-user files; the WAL journal mode is used
  so readers are not blocked by the writer.

## Shared Registry Database

Alongside the per-user databases the gateway keeps one small shared database with a registry
role (see [ADR-0008](../decisions/0008-shared-registry-db.md)): the user registry with login
credentials, the API-key index, operator accounts, gateway-wide settings, the asset catalog
and the watcher's per-network service state
(see [ADR-0010](../decisions/0010-networks-assets-financial-data.md)). It holds no financial
data — wallets, addresses, bindings and operations live only in the user databases.

## Backends

- **SQLite** — the first backend: the user's database is a file in the user's directory.
- **PostgreSQL, MySQL** — planned next. How per-user isolation maps onto a server DBMS
  (database per user vs schema per user) is an open question, to be decided in a dedicated
  ADR when PostgreSQL support is added.

## Migrations

Schema migrations run across all user databases in a loop; the migration tooling must
support this from the start. The choice of implementation (SQLAlchemy Core + Alembic vs a
hand-rolled thin layer) is an open question, to be decided in a dedicated ADR at
implementation time (new dependencies require approval).

## Related

- [Architecture Overview](../overview.md)
- [Orchestrator](orchestrator.md)
- [Watcher](watcher.md)
- [Data Model](../data-model.md)
- [ADR-0005: Multi-User Model](../decisions/0005-multi-user-model.md)
- [ADR-0006: Storage Abstraction](../decisions/0006-storage-abstraction.md)
