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
  the family; otherwise the wallet's next free index is allocated. Within one network an
  index is unique per wallet (schema level), and allocation is serialized per user — two
  payers can never receive one address.
- No financial data.

### Applications

The unit of connection to the gateway (see
[ADR-0009](decisions/0009-address-bindings-primary-mode.md)): belongs to the gateway user,
identified by its API key (the key-hash → user index lives in the shared registry). Carries
the "network → wallet" mapping configured by the owner in the cabinet — applications
themselves never see wallets (see
[ADR-0011](decisions/0011-application-api-principles.md)).

### Application Users

Internal id, application, instance, external identifier — an arbitrary string supplied by
the application, opaque to the gateway.

- The **instance** is the namespace of external identifiers
  (see [ADR-0025](decisions/0025-application-instances.md)): independent installations of
  one application share an API key, and their own user numbering collides. Identity is
  therefore the triple "application + instance + external identifier", unique at the schema
  level.
- The instance is an arbitrary string the gateway does not interpret, exactly like the
  external identifier. An application with a single installation uses the **empty**
  instance — an empty string rather than NULL, for the same reason as the memo of a
  binding: NULL does not equal NULL in SQL, and uniqueness with it does not work.
- Bindings reference the internal id of this row, so the instance separates payers without
  appearing in the binding table at all.

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
(see [ADR-0017](decisions/0017-universal-tx-model.md),
[ADR-0021](decisions/0021-two-phase-scanning.md)): address, transaction id, asset,
direction, event index, **counterparty (the other side of the transfer — what makes a
move between one's own addresses recognizable by fact rather than by a matching amount,
see [ADR-0027](decisions/0027-gateway-fee-billing.md))**, amount, **block number
(required — on-chain means in a block)**,
time, **execution status (success / failed)**, first-seen time, the **finalization
marker** (set when the authoritative scan of the finalized zone confirms the row) and a
service marker **"applied to balance"** — set exactly once, in the same database
transaction as the balance update.

- A row without the finalization marker is **provisional** — observed above the finality
  boundary; it shows as pending, never touches balances, and is deleted if the finalized
  chain does not confirm it (reorganization). Finalized rows are append-only.
- Failed rows (in a block, execution failed — e.g. REVERT / out of energy) never affect
  balances; kept for dispute diagnostics.
- Idempotency: re-scanning the same ranges must not duplicate rows — unique key
  "transaction id + address + asset + direction + event index" (the direction covers a
  transfer to self; the event index covers several transfers of one asset inside one
  transaction, e.g. batch payouts).

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

Operator panel sessions are a separate table of the same design (1-day expiry): the
operators' and users' cookies and route groups never overlap. Password-reset tokens —
the fingerprint of the one-time token from the email with an expiry; a user holds at
most one active token.

### Asset Catalog

Network-specific reference data: network, native coin or token contract, symbol, decimals.
USDT on TRON and USDT on Ethereum are two different assets; a chain family (e.g. EVM) never
appears here — only concrete networks do. The catalog is filled automatically when the
watcher first observes a new asset: the gateway does not restrict the set of accepted
assets — it honestly records everything that arrives, filtering is the consumer's business.

### Wallet Xpub Index

Gateway-wide uniqueness of attached xpubs (one xpub — one wallet), by analogy with the
API-key index: the SHA-256 fingerprint of the xpub and the owning user. The xpub itself
lives only in the owner's database; a duplicate attachment is rejected with the same
neutral error as a broken key, so the response does not reveal that the xpub is already
attached elsewhere.

### Watcher Service State

Per network: the cursors of the authoritative finalized scan
(see [ADR-0018](decisions/0018-range-scanning.md),
[ADR-0021](decisions/0021-two-phase-scanning.md)) — the last finalized block processed
and the time cursor of the token scan; scanning resumes from them with an overlap, and
duplicates are extinguished by the transactions unique key. Not financial data.

## The Billing Database

The owner's billing data — the fee they take from the users (see
[ADR-0027](decisions/0027-gateway-fee-billing.md)). A separate database, because the
registry is no place for financial data, while a user's database is moved, restored from a
copy and carried into the archive together with them.

> Planned by ADR-0027; not implemented yet.

### The owner's master wallets

One per payment network: the network, an account-level xpub, the time it was entered. The
key is watch-only, exactly like the users' ones; gateway-wide xpub uniqueness covers it
too — a collision would give one address two owners at once, and the watcher would not
know whose incoming payment it is.

### Invoice addresses

A permanent mapping "user + payment network → address + derivation index". The address is
issued once and serves every later invoice of that user: a payment is attributed by the
receiving address, and the address count stays equal to the number of paying users rather
than to the number of invoices issued. Next to it sits the mark of how far the address has
been checked with the provider: the next poll resumes from there instead of re-reading the
address's whole history.

### Invoices

One row per "user + period", unique at the schema level: the period bounds, the period's
turnover, the rate and threshold applied, the amount due, the due date, the payment
network and address, the state (issued / paid / overdue), the amount credited, the times
of issue and payment, and the mark of a manual confirmation — the operator and the reason.

- The billing money values are integers in **micro-USDT** (the sixth decimal place, the
  way USDT itself is denominated in TRON), stored as strings like every amount in the
  gateway.

- The rate, the threshold and the term are a snapshot of the settings as the invoice was
  issued: a later change of the settings never rewrites invoices that already exist.
- The turnover is stored as a total: it is the result of a calculation over the user's
  transactions, not a reference to them.

### Invoice payments

Transfers observed on invoice addresses: the address, the transaction id, the asset, the
amount, the time and the finalization marker — plus the crediting: which invoice it went
to and how much of it counted.

- The amount is kept twice: in the minimal units of the asset that arrived (as is, for the
  record of observations) and valued in micro-USDT, the invoice's unit. A zero valuation
  means "not invoice money" — that is what a stray token on an invoice address looks like.
- Crediting goes in order: an address's money covers invoices from the oldest to the
  newest, and the leftover stays uncredited on the payment — that leftover is the credit
  the next invoice draws on. A manual confirmation by the operator lives in the invoice
itself rather than here: it has neither a transaction nor an asset, and a synthetic
payment row would only muddy the record of observations.

- An overpayment stays an uncredited remainder and counts against the next invoice; there
  is no separate "credit balance" entity — that state follows from the payments.
- A foreign asset arriving on an invoice address is recorded but attributed to no invoice.

### A user's billing state

The payment network (TRON by default) and the access state: in order, or suspended for
non-payment, with the time of suspension. The state is independent of the account status
in the registry — an administrative block and a suspension for non-payment are each
lifted by their own action.

### What counts as turnover

The user's successful, finalized incoming operations in the assets from the operator's
list (contract addresses, not symbols). Moving funds between one's own addresses is not
income: such an operation has an outgoing leg of the same user, in the same transaction,
for the same amount of the same asset — that is enough to recognize it, and no
counterparty has to be stored in the transaction for it.

## Related

- [Storage Layer](components/storage.md)
- [Watcher](components/watcher.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](decisions/0009-address-bindings-primary-mode.md)
- [ADR-0010: Networks, Assets and Financial Data Structures](decisions/0010-networks-assets-financial-data.md)
- [ADR-0027: The Gateway Owner's Fee](decisions/0027-gateway-fee-billing.md)
