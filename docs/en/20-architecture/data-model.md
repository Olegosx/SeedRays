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
- The derivation index is reused across networks for the same "wallet + application user"
  pair — an EVM-family wallet then gives the payer the same address in every network of
  the family; otherwise the wallet's next free index is allocated.
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

- Derived data: updated in the same database transaction that marks a transaction row as
  applied, and recomputable from the transactions table at any moment.
- Counts finalized successful transactions only; what still awaits finality is a
  computation over the transactions table, and queued payments are visible in the mempool
  queue.

### Transactions (on-chain)

One table for transactions that made it into the chain
(see [ADR-0017](decisions/0017-universal-tx-model.md)): address, transaction id, asset,
direction, amount, **block number (required — on-chain means in a block)**, time,
**execution status (success / failed)**, first-seen time, and a service marker
**"applied to balance"** — set exactly once, in the same database transaction as the
balance update. Rows are otherwise append-only.

- Confirmed / awaiting finality is **computed** against the network's finality boundary
  (TRON: solidification) — never stored.
- Failed rows (in a block, execution failed — e.g. REVERT / out of energy) never affect
  balances; kept for dispute diagnostics.
- Idempotency: re-scanning the same ranges must not duplicate rows — unique key
  "transaction id + address + asset" (to be refined at implementation if one transaction
  can carry several transfers of the same asset to the same address).
- Provisional rows that never reach finality (chain reorganizations) are cleaned by a
  policy defined at watcher implementation.

### Mempool Queue

Observations of queued transactions — broadcast but not included in any block
(see [ADR-0017](decisions/0017-universal-tx-model.md)): no block number; rows may linger
(EVM low-fee transactions) or vanish. A universal entity of the model: populated only when
the network's data source can see the queue (TRON's indexed providers cannot; EVM sources
and self-hosted nodes can).

## Shared Registry Database

Alongside the user registry, the API-key index, operator accounts and gateway settings
(see [ADR-0008](decisions/0008-shared-registry-db.md)):

### User Emails

Addresses attached to an account (the primary one + added ones): the address (unique
across the gateway, lowercase), the "primary" flag, the confirmation time. Confirmation
goes through a one-time token from the email
([ADR-0020](decisions/0020-mail-provider.md)): the database keeps only the token's
SHA-256 fingerprint and its expiry.

### Cabinet Sessions

The browser cookie carries a random token; the database keeps its SHA-256 fingerprint,
the user, the expiry (7 days, 30 with "remember me") and the CSRF token checked against
the `X-CSRF-Token` header of every mutating request. Expired rows are purged on sign-in.

### Asset Catalog

Network-specific reference data: network, native coin or token contract, symbol, decimals.
USDT on TRON and USDT on Ethereum are two different assets; a chain family (e.g. EVM) never
appears here — only concrete networks do. The catalog is filled automatically when the
watcher first observes a new asset: the gateway does not restrict the set of accepted
assets — it honestly records everything that arrives, filtering is the consumer's business.

### Watcher Service State

Per network: the height of the last processed block and the time of the last scan — the
range cursor of [ADR-0018](decisions/0018-range-scanning.md); scanning resumes from it
with an overlap, and duplicates are extinguished by the transactions unique key. Not
financial data.

## Related

- [Storage Layer](components/storage.md)
- [Watcher](components/watcher.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](decisions/0009-address-bindings-primary-mode.md)
- [ADR-0010: Networks, Assets and Financial Data Structures](decisions/0010-networks-assets-financial-data.md)
