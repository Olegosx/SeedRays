[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0010-networks-assets-financial-data.md)

# ADR-0010: Networks, Assets and Financial Data Structures

**Status:** accepted; the three-table transaction part is superseded by
[ADR-0017](0017-universal-tx-model.md) — a universal two-table model (on-chain + mempool
queue); networks, assets, computed confirmations and derived balances remain in force

## Context

A wallet of the EVM family yields the same address in every EVM network, so an address alone
does not say where a payment happened. Tokens complicate it further: USDT on TRON and USDT
on Ethereum are different units of value. The gateway must record incoming funds honestly,
track confirmations cheaply and keep its ledger immutable.

## Decision

- A wallet carries a **chain family** (TRON, EVM, …) — the address and derivation standard.
  A binding names the **concrete network**; binding uniqueness becomes "wallet + network +
  application + application user" (refines [ADR-0009](0009-address-bindings-primary-mode.md)).
- An **asset** is network-specific reference data: network, native coin or token contract,
  decimals. Transactions and balances reference an asset; the network follows from it — the
  financial tables need no separate network column, and a chain family never appears in
  data, only concrete networks do.
- **Three transaction tables**: pending (in flight — the only mutable one, always small),
  confirmed (append-only ledger), failed (append-only, never affects balances). A
  transaction leaves pending atomically: removal, insertion into the final table and the
  balance update happen in one database transaction.
- **The block number is stored per transaction** (the block it was included in) and never
  changes; confirmation counts are **computed** as "current network height − block number".
  The per-network height — the last processed block — lives in the shared registry as the
  watcher's service state and doubles as the scan resume point (extends
  [ADR-0008](0008-shared-registry-db.md)).
- **Balances are derived**: one row per "address + asset", updated in the same database
  transaction as the confirmed insert, always recomputable from the confirmed ledger;
  confirmed transactions only.
- **No accepted-asset restriction**: the gateway records everything that arrives; the asset
  catalog is auto-populated on first observation. Filtering is the consumer's business.

## Alternatives Considered

- **A stored per-row confirmation counter:** rejected — every watcher pass would update
  every pending row; the computed count updates one number per network instead.
- **Network-agnostic assets ("USDT" without a network):** rejected — amounts in different
  networks are not interchangeable.
- **One transactions table with a status column:** rejected — the ledger must be
  append-only; a mutable ledger invites update anomalies.
- **A whitelist of accepted assets:** rejected — the gateway reports honestly what arrived;
  if the application or its user wants to ignore something, that is their decision.

## Consequences

- The asset catalog grows automatically; spam tokens sent to gateway addresses will appear
  in it and in the recorded data — consumers filter what they care about.
- The registry ([ADR-0008](0008-shared-registry-db.md)) gains the asset catalog and the
  watcher state; the binding ([ADR-0009](0009-address-bindings-primary-mode.md)) gains the
  network column.
- Computing confirmations costs one height lookup per network, regardless of how many
  transactions are pending.

## Related

- [Data Model](../data-model.md)
- [Watcher](../components/watcher.md)
- [ADR-0008: Shared Registry Database](0008-shared-registry-db.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](0009-address-bindings-primary-mode.md)
