[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0009-address-bindings-primary-mode.md)

# ADR-0009: Persistent Address Bindings as the Primary Mode

**Status:** accepted; refined by [ADR-0010](0010-networks-assets-financial-data.md) — the
binding gains a network column, uniqueness includes the network

## Context

The gateway's primary direction is not issuing single invoices but permanently binding
payment addresses to accounts of connected applications. A connected application has its own
users; its user gets a permanent binding to a payment address, and the application owner
reads the state of that user's internal balance through the operations observed on the
address. Single invoices will be implemented later.

## Decision

- An **application** is the unit of connection to the gateway: it belongs to a gateway user
  and is identified by its API key.
- The core entity is the **binding**: wallet (the one whose xpub the address is derived
  from), address, memo (reserve field for networks that use memos), application, application
  user.
- **Application users are a separate table.** The application-user identifier is an
  arbitrary string supplied by the application; the gateway does not interpret it and
  guarantees its uniqueness within the application ("user1" in application A and "user1" in
  application B are unrelated). Bindings reference the internal id of the application-user
  record.
- **The binding keeps a direct reference to the application as well** — a deliberate
  denormalization: filtering bindings by application is an elementary operation and must not
  require going through the application-user table. The agreement of the two references is
  enforced at the schema level.
- **Binding uniqueness:** wallet + application + application user → one binding. In memo
  networks many bindings may share one address and differ by memo, so a binding is
  identified by address + memo, not by the address alone.
- The binding table is a **mapping table and holds no financial data** — no balance column.
  The source of truth about funds is the record of operations observed on the addresses;
  balances are derived from it (structures — at the data design step).

## Alternatives Considered

- **Invoice-based model as the primary mode:** deferred, not rejected — single invoices will
  be added later on top of the same structures.
- **A balance column in the binding:** rejected — financial data does not belong in a
  mapping table.
- **Repeating the application-user string in every binding instead of a separate table:**
  rejected — uniqueness would have to be enforced in many places, the string would be
  duplicated per binding, and future application-user attributes would have nowhere to live.

## Consequences

- The watcher's work list is the list of bindings — consistent with
  [ADR-0007](0007-address-centric-watcher.md).
- Requesting a binding for an application user is an Application API operation
  (see [HTTP API](../components/http-api.md)).
- The detailed structure of the financial data (balances, transactions) is the next design
  step.

## Related

- [HTTP API](../components/http-api.md)
- [Watcher](../components/watcher.md)
- [Functional Requirements](../../10-requirements/functional.md)
