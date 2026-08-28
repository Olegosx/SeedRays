[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0008-shared-registry-db.md)

# ADR-0008: Shared Registry Database

**Status:** accepted; extended by [ADR-0010](0010-networks-assets-financial-data.md) — the
registry also holds the asset catalog and the watcher's per-network service state

## Context

Per-user databases ([ADR-0005](0005-multi-user-model.md)) leave no natural home for
gateway-global data: looking up a user by login at sign-in, instantly resolving an API key to
its owner, operator accounts, gateway-wide settings (watcher intervals, data provider access
keys — e.g. one TronGrid key for the whole gateway). Distributing these across user databases
would mean scanning N databases per lookup or encoding an index into filesystem naming
conventions.

## Decision

The gateway keeps one small shared database with a registry role:

- user registry: identifier, status, login credentials, reference to the user's directory;
- API-key index: key hash → owning user;
- operator (superadmin) accounts;
- gateway-wide settings.

It contains no financial data: wallets, addresses, bindings and operations live only in the
user databases.

## Alternatives Considered

- **No shared database** (directory-naming conventions for login lookup, user id embedded
  into API keys): workable, but reinvents a registry scattered across filesystem conventions;
  rejected.
- **One shared database for everything:** already rejected in
  [ADR-0005](0005-multi-user-model.md) — structural isolation of user data.

## Consequences

- Sign-in and key resolution are a single registry lookup followed by opening the one user
  database concerned.
- Compromise or corruption of the registry exposes no payment data; a user database remains
  a self-contained unit of backup and transfer.
- The registry schema is migrated separately from the user database schema.

## Related

- [Storage Layer](../components/storage.md)
- [ADR-0005: Multi-User Model](0005-multi-user-model.md)
