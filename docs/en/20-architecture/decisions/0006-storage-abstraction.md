[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0006-storage-abstraction.md)

# ADR-0006: Storage Abstraction at the Domain-Operation Level

**Status:** accepted; the open library question is closed by
[ADR-0013](0013-backend-stack.md) — SQLAlchemy Core + Alembic

## Context

The gateway starts on SQLite and will later support PostgreSQL and MySQL. Business logic must
not depend on the chosen database.

## Decision

Data access goes through a storage interface defined at the level of domain operations
("save an incoming payment", "list the wallet's addresses", "mark an address as used").
Components above the interface — orchestrator, watcher — never see SQL. Each backend
implements the operations in its own dialect. First backend: SQLite (one database file per
user, WAL journal mode); PostgreSQL and MySQL follow.

## Alternatives Considered

- **A universal SQL pass-through as the abstraction:** rejected — it leaks the database into
  the business logic and makes backends harder to substitute; the same principle already
  applies to the chain abstraction.
- **Direct database calls from components:** rejected — ties logic to one database and makes
  the planned PostgreSQL/MySQL support a rewrite.

## Open Questions

- Implementation library: SQLAlchemy Core + Alembic (one code path for all three databases,
  migration tooling included; a new dependency requiring approval) vs a hand-rolled thin
  layer over standard drivers (no dependencies; three dialects and migrations maintained by
  hand). To be decided in a dedicated ADR at implementation time.
- Mapping of per-user isolation onto PostgreSQL/MySQL — see
  [ADR-0005](0005-multi-user-model.md).

## Consequences

- The database can be switched without touching business logic.
- Storage operations are testable against an in-memory or temporary SQLite backend.
- The storage interface is the single place where the schema is known.

## Addendum (2026-09-06)

The code audit found the orchestrator bypassing the interface with direct SQL; the
decision is confirmed and enforced — every orchestrator module now goes through the
storage layer. Two clarifications, so the layer does not degenerate into a mirror of its
callers:

- Operations are named and shaped by **domain meaning** ("record a transaction",
  "allocate the next derivation index"), never by the SQL they happen to contain.
- The interface legitimately includes **screen-shaped read operations** — one wide,
  purpose-named read per screen or API read path (the dashboard summary, the history
  page). A need the layer cannot express is a reason to extend the layer or open a new
  ADR — never to bypass it.

## Related

- [Storage Layer](../components/storage.md)
- [ADR-0005: Multi-User Model](0005-multi-user-model.md)
