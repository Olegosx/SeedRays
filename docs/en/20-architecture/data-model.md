[Index](../index.md) · [Architecture Overview](overview.md) · [Storage Layer](components/storage.md) · [Русская версия](../../ru/20-architecture/data-model.md)

# Data Model

Conceptual structure of the gateway's data: entities, their fields and the rules that bind
them. Exact table definitions (types, indexes) are settled at implementation time. Where the
data lives is set by [ADR-0005](decisions/0005-multi-user-model.md) and
[ADR-0008](decisions/0008-shared-registry-db.md); networks, assets and the financial
structures — by [ADR-0010](decisions/0010-networks-assets-financial-data.md).

## User Database

### Bindings

The core mapping table (see
[ADR-0009](decisions/0009-address-bindings-primary-mode.md)): wallet, network, address,
memo, application, application user.

- One binding per "wallet + network + application + application user". A wallet carries a
  chain family (TRON, EVM, …); the binding names the concrete network.
- In memo networks a binding is identified by "address + memo"; many bindings may share one
  address.
- The application reference is kept alongside the application-user reference on purpose
  (cheap filtering by application); the agreement of the two is enforced at the schema
  level.
- No financial data.

### Applications

The unit of connection to the gateway (see
[ADR-0009](decisions/0009-address-bindings-primary-mode.md)): belongs to the gateway user,
identified by its API key (the key-hash → user index lives in the shared registry). Carries
the "network → wallet" mapping configured by the owner in the cabinet — applications
themselves never see wallets (see
[ADR-0011](decisions/0011-application-api-principles.md)).

### Application Users

Internal id, application, external identifier — an arbitrary string supplied by the
application, opaque to the gateway. Unique within the application.

### Balances

One row per "address + asset": current balance, total received, date/time of the last
deposit.

- Derived data: updated in the same database transaction as the confirmed-transaction
  insert, and recomputable from the confirmed-transactions table at any moment.
- Counts confirmed transactions only; what is still pending is visible in the pending
  table.

### Transactions

Three tables with a shared core of fields: address, transaction id, asset, direction,
amount, block number (the block the transaction was included in — written once, never
updated), date/time.

- **Pending** — transactions in flight; the only mutable table, always small. Extra field:
  status. Confirmation counts are not stored — they are computed as "current network
  height − transaction's block number".
- **Confirmed** — final successes; an append-only ledger, rows are never updated.
- **Failed** — final failures; append-only, kept for diagnostics, never affects balances.

Rules:

- A transaction leaves Pending atomically: one database transaction removes it from the hot
  table, inserts it into Confirmed or Failed and (for Confirmed) updates the balance row.
- Idempotency: re-scanning the same blocks must not duplicate rows — unique key
  "transaction id + address + asset" (to be refined at implementation if one transaction
  can carry several transfers of the same asset to the same address).

## Shared Registry Database

Alongside the user registry, the API-key index, operator accounts and gateway settings
(see [ADR-0008](decisions/0008-shared-registry-db.md)):

### Asset Catalog

Network-specific reference data: network, native coin or token contract, symbol, decimals.
USDT on TRON and USDT on Ethereum are two different assets; a chain family (e.g. EVM) never
appears here — only concrete networks do. The catalog is filled automatically when the
watcher first observes a new asset: the gateway does not restrict the set of accepted
assets — it honestly records everything that arrives, filtering is the consumer's business.

### Watcher Service State

Per network: the height of the last processed block. Serves both as the scan resume point
after a stop and as the height from which confirmation counts are computed. Not financial
data.

## Related

- [Storage Layer](components/storage.md)
- [Watcher](components/watcher.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](decisions/0009-address-bindings-primary-mode.md)
- [ADR-0010: Networks, Assets and Financial Data Structures](decisions/0010-networks-assets-financial-data.md)
