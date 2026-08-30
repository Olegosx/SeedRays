[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0017-universal-tx-model.md)

# ADR-0017: Universal Transaction Model — Two Tables by Physical Location

**Status:** accepted

## Context

The three-table model of [ADR-0010](0010-networks-assets-financial-data.md)
(pending / confirmed / failed) mixed physically different things. A transaction has exactly
two physical locations: the **queue** (mempool — broadcast but not in any block; on EVM
networks a low-fee transaction can linger there for hours) and the **chain** (included in a
block). An in-block transaction has two independent attributes: the **execution outcome**
(success, or failure such as REVERT / out of energy — the transaction is on-chain forever,
the fee is spent, but the transfer did not happen) and **finality** (a computed boundary —
TRON's solidification — not a state). The old "pending" table conflated the queue with
"in a block, awaiting finality", and "failed" promoted an attribute to an entity. The model
must be universal across all chains.

## Decision

- **`transactions`** — on-chain transactions only: address, transaction id, asset,
  direction, amount, **block number (required)**, time, **execution status
  (success / failed)**, first-seen time, and a service marker **"applied to balance"** —
  set exactly once, in the same database transaction as the balance update. Rows are
  otherwise append-only.
  - Confirmed / awaiting finality is **computed** against the network's finality boundary,
    never stored.
  - Failed rows never affect balances; they are kept for dispute diagnostics
    ("I paid, here is the hash" — the hash shows a failed execution).
  - Idempotency: unique key "transaction id + address + asset".
- **`mempool_queue`** — observations of queued transactions: no block number; rows may
  linger or vanish. A universal entity of the model: it is populated only when the
  network's data source can see the queue (TRON's indexed providers cannot; EVM sources
  and self-hosted nodes can).
- **`balances`** stays as the cached aggregate of
  [ADR-0010](0010-networks-assets-financial-data.md), updated transactionally together
  with the marker.

## Alternatives Considered

- **The previous three tables:** rejected — moving rows between tables models a computed
  boundary as data movement, and "failed" is an attribute, not an entity.
- **No queue table at all:** rejected — the queue is physically real on EVM-family
  networks, and the model must be universal.
- **Fully computed balances without a cache:** workable at our scale, but rejected — cheap
  mass balance reads for applications remain a requirement.

## Consequences

- The user-database schema is reworked before the first release (the initial migration is
  rewritten — no deployment exists anywhere).
- The watcher classifies rows against the boundary instead of moving them between tables.
- Provisional rows that never reach finality (chain reorganizations) need a cleanup
  policy — defined at watcher implementation.

## Related

- [Data Model](../data-model.md)
- [Watcher](../components/watcher.md)
- [ADR-0010: Networks, Assets and Financial Data Structures](0010-networks-assets-financial-data.md)
- [ADR-0018: Range Scanning as the Watcher's Primary Acquisition](0018-range-scanning.md)
