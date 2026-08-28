[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0005-multi-user-model.md)

# ADR-0005: Multi-User Model — Per-User Database and Directory, Three Roles

**Status:** accepted; extended by [ADR-0008](0008-shared-registry-db.md) — a small shared
registry database for non-financial gateway-global data

## Context

The gateway serves multiple users. Multi-user support affects the base architecture (storage
layout, watcher scheduling, API access model), so deferring it until after an MVP would mean
threading the user context through finished single-user code — expensive and error-prone
(a missed check exposes another user's data).

## Decision

- The gateway is multi-user from the start, implemented fully (including interfaces), not as
  a post-MVP retrofit. Every orchestrator operation and every storage call carries the user
  context.
- Each user has their own database (own set of tables) and own working directory.
- A user owns one or more wallets. A wallet is an entity with its own xpub; addresses,
  derivation and watching are tied to a wallet, not to the user directly.
- Access is split into three roles:
  1. **Gateway operator (superadmin)** — manages users and gateway-wide settings.
  2. **User (wallet owner)** — has a personal cabinet; manages own wallets and own
     application keys.
  3. **User's applications** — access data and request payment addresses with an API key,
     scoped to the owning user's wallets.

## Alternatives Considered

- **Shared database with a user column:** rejected — isolation would rest on discipline
  (every query must filter by user); per-user databases make the isolation structural.
- **Single-user MVP, multi-user later:** rejected — retrofitting the user context is more
  expensive than building with it from the start.

## Consequences

- Structural data isolation; a user's backup, transfer or deletion is an operation on one
  directory.
- Schema migrations run across all user databases in a loop; tooling must support this from
  the start.
- Gateway-wide views (operator statistics) fan out over N databases.
- How per-user isolation maps onto PostgreSQL/MySQL (database per user vs schema per user)
  is an open question for a future ADR.
- The HTTP API gets three route groups — one per role; this extends
  [ADR-0004](0004-two-api-groups.md).
- Write contention in SQLite is spread across per-user files (see
  [Storage Layer](../components/storage.md)).

## Related

- [Storage Layer](../components/storage.md)
- [HTTP API](../components/http-api.md)
- [Watcher](../components/watcher.md)
- [ADR-0006: Storage Abstraction](0006-storage-abstraction.md)
- [ADR-0007: Address-Centric Watcher](0007-address-centric-watcher.md)
