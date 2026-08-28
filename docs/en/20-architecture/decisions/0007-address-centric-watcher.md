[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0007-address-centric-watcher.md)

# ADR-0007: Address-Centric Watcher Pass

**Status:** accepted

## Context

With multiple users the watcher must observe all users' wallets. Data provider rate limits
are global for the gateway (the provider counts requests per IP/key regardless of whose
addresses are queried), some providers offer batch endpoints, and no user should be starved
by another user's large address set.

## Decision

The watcher thinks in addresses, not in users. Before a pass it collects a single work list
of tracked addresses from all users, each tagged with its owner (user, wallet) and chain.
The pass processes the list grouped by chain and provider, with centralized request pacing
per provider and fair ordering across users. Failures are isolated per address/user within
the pass. Results are written to each owner's database.

## Alternatives Considered

- **A loop over users, checking each user's addresses in turn:** rejected — smaller request
  batches, no centralized control over provider rate limits, and pass time grows with users
  regardless of address counts.
- **A background task per user:** rejected — independent tasks collectively exceed the
  shared provider limits, fairness is unsolved without an extra scheduler, and complexity
  grows with the user count.

## Consequences

- Rate limiting and batching live in one place, per provider.
- The watcher stays a single supervised component (consistent with
  [ADR-0003](0003-single-process-supervised.md)); a task per chain is the natural next step
  if the number of chains grows.
- The work-list collection step is the only place that knows about users; the pass itself is
  tenant-agnostic.

## Related

- [Watcher](../components/watcher.md)
- [Chain Abstraction](../components/chain-abstraction.md)
- [ADR-0005: Multi-User Model](0005-multi-user-model.md)
