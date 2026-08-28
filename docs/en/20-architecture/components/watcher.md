[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/watcher.md)

# Watcher

Monitors balances and incoming payments across all users' wallets.

## Structure

Built library-first (see [ADR-0001](../decisions/0001-library-first-core.md)):

- **Single pass** — the core operation: query the data source for the tracked addresses,
  record new incoming payments and balance changes.
- **Continuous mode** — a loop calling the single pass at a configured interval; runs as a
  background thread/task launched and supervised by the [Orchestrator](orchestrator.md)
  (see [ADR-0003](../decisions/0003-single-process-supervised.md)).
- **Console command** — a one-shot check for debugging and manual verification, reusing the
  same single-pass code.

## Multi-User Pass

The watcher thinks in addresses, not in users
(see [ADR-0007](../decisions/0007-address-centric-watcher.md)):

- Before a pass it collects a single work list of tracked addresses from all users, each
  tagged with its owner (user, wallet) and its chain.
- The pass processes the list grouped by chain and data provider: provider rate limits are
  global for the gateway, so request pacing is centralized, and batch endpoints can be used
  where a provider offers them.
- Fair ordering: a user with many addresses must not starve users with few.
- Failures are isolated per address/user within the pass: they are logged and do not
  interrupt the pass for everyone else.
- Results are written to each owner's database via the [Storage Layer](storage.md).

## Data Access

All blockchain access goes through the [Chain Abstraction](chain-abstraction.md): public
provider APIs first, a self-hosted node (RPC) later — without changes to watcher logic.

The watcher keeps its service state — the height of the last processed block per network —
in the shared registry database: it is both the scan resume point after a stop and the
height from which confirmation counts are computed
(see [ADR-0010](../decisions/0010-networks-assets-financial-data.md)).

## Interfaces

_To be defined._

## Related

- [Architecture Overview](../overview.md)
- [Chain Abstraction](chain-abstraction.md)
- [Storage Layer](storage.md)
- [Orchestrator](orchestrator.md)
- [Monitoring](../../40-operations/monitoring.md)
