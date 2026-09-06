[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0021-two-phase-scanning.md)

# ADR-0021: Two-Phase Scanning — Finalized Authority with a Provisional Preview

**Status:** accepted, refines ADR-0017 and ADR-0018

## Context

[ADR-0017](0017-universal-tx-model.md) left two questions to the watcher implementation:
the cleanup policy for provisional rows that never reach finality (chain
reorganizations), and the refinement of the idempotency key if one transaction can carry
several transfers of the same asset to the same address (real for batch payouts). The
first implementation scanned up to the chain head and applied rows to balances by block
number alone: a transfer observed in a block that was later reorganized out of the chain
would still be counted as a confirmed payment — the worst error class for a payment
gateway. At the same time, waiting for finality before recording anything would delay the
pending status by about a minute (TRON solidification, ~19 blocks), which the product
does not want.

## Decision

Each pass scans a network in two phases:

- **Authoritative scan** — reads only the finalized zone: native blocks up to the
  finality boundary, token events with the provider's confirmed-only filter. Its rows
  carry a **finalization marker** and are the **only** source of balance changes. The
  range cursors (`watcher_state`) belong to this phase alone.
- **Provisional preview** — reads the zone above the boundary (native blocks
  boundary+1…head, token events with the unconfirmed-only filter) so an incoming payment
  is visible as pending immediately. Its rows carry no finalization marker, never touch
  balances, and the phase is best-effort: its failure is logged and never blocks the
  authoritative progress; the zone is stateless and rescanned every pass.

Reconciliation ties the phases together: when the authoritative scan covers a block
range, a provisional row inside that range is either **promoted** (the finalized chain
confirmed it; block, time and status are refreshed — a reorganization may have moved it)
or **deleted with a warning log** (the finalized chain never confirmed it). This is the
cleanup policy ADR-0017 called for.

The idempotency key is refined to **"transaction id + address + asset + direction +
event index"**: the direction distinguishes the two legs of a transfer to self, the
event index (from the provider's event log, 0 for native transfers) distinguishes
several transfers of the same asset inside one transaction.

## Alternatives Considered

- **Scan only the finalized zone:** simplest — no cleanup machinery at all — but the
  pending status appears ~1 minute late. Rejected by the product owner: immediate
  pending visibility is worth the reconciliation.
- **Record up to the head and verify each transaction individually at finality:**
  rejected — a reorganization can also *introduce* transactions the head-zone scan never
  saw (the canonical block differs from the orphaned one), so point checks of known
  rows cannot restore completeness; only a re-scan of the finalized range can.
- **Keep the single-phase scan and accept the risk:** rejected — counting a reorged-out
  transfer as a confirmed payment is unacceptable for a payment gateway.

## Consequences

- Every finalized block range is fetched twice (once as preview, once authoritatively) —
  a bounded, predictable increase in provider traffic; the preview phase adds roughly
  one block-range request and one events request per contract per pass.
- The transactions table carries `event_index` and `finalized_at`; balances apply on the
  finalization marker instead of comparing block numbers.
- A provisional (pending) row may disappear from history — exactly when the chain
  discarded the transaction; the log records the removal.
- The chain-source interface takes a confirmed/unconfirmed mode for token events and
  reports the event index.

## Related

- [Data Model](../data-model.md)
- [Watcher](../components/watcher.md)
- [Chain Abstraction](../components/chain-abstraction.md)
- [ADR-0017: Universal Transaction Model](0017-universal-tx-model.md)
- [ADR-0018: Range Scanning as the Watcher's Primary Acquisition](0018-range-scanning.md)
