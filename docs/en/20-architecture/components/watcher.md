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
(see [ADR-0007](../decisions/0007-address-centric-watcher.md)), and acquires data by
ranges, not by addresses (see [ADR-0018](../decisions/0018-range-scanning.md)):

- Before a pass it collects the binding set of all users, each entry tagged with its owner
  (user, wallet) and network. This set is the **local match filter**.
- Per network the pass fetches the head and the finality boundary once, then all token
  Transfer events of the active contracts and the native-transfer block range since the
  cursor — a cost independent of the number of addresses — and matches transfers against
  the binding set locally. The provider never learns the gateway's address set.
- Matched rows land in the transactions table; a success row crossing the finality
  boundary is applied to the balance in one database transaction with its marker.
- Per-address indexed queries remain for targeted tasks: the initial history of a new
  binding, spot reconciliation.
- Failures are isolated within the pass and logged; a provider "slow down" answer pauses
  that network's scan until the next pass.
- Results are written to each owner's database via the [Storage Layer](storage.md).

## Data Access

All blockchain access goes through the [Chain Abstraction](chain-abstraction.md): public
provider APIs first, a self-hosted node (RPC) later — without changes to watcher logic.

The watcher keeps its service state — the height of the last processed block per network —
in the shared registry database: it is both the scan resume point after a stop and the
height from which confirmation counts are computed
(see [ADR-0010](../decisions/0010-networks-assets-financial-data.md)).

## Interfaces

- Module: the single pass (`run_pass`) and the continuous loop over it; the loop is started
  and supervised by the orchestrator.
- Console command `seedrays watch` — one pass; the data directory comes from the
  `SEEDRAYS_DATA_DIR` environment variable (the bootstrap layer of
  [ADR-0016](../decisions/0016-config-layers.md)).
- Settings (registry): the provider API key and request rate, the pass interval, the
  cursor overlap, the scan start moment, and the watched token contracts per network
  (`watcher.contracts.<network>` — a JSON list of contract/symbol/decimals entries).
- The first pass of a network scans from the gateway's first launch — operations that
  happened before the gateway existed are of no interest to it; the `watcher.scan_start`
  setting overrides this explicitly.

## Related

- [Architecture Overview](../overview.md)
- [Chain Abstraction](chain-abstraction.md)
- [Storage Layer](storage.md)
- [Orchestrator](orchestrator.md)
- [Monitoring](../../40-operations/monitoring.md)
