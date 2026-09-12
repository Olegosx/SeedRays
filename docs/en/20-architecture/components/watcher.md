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
- Per network the pass fetches the head and the finality boundary once, then scans in
  two phases (see [ADR-0021](../decisions/0021-two-phase-scanning.md)) — a cost
  independent of the number of addresses; transfers are matched against the binding set
  locally, and the provider never learns the gateway's address set:
  - the **authoritative scan** reads only the finalized zone (native blocks up to the
    boundary, confirmed-only token events) from its cursors; its rows carry the
    finalization marker and are the only source of balance changes — a finalized success
    row is applied to the balance in one database transaction with its marker;
  - the **provisional preview** reads the zone above the boundary so a payment shows up
    as pending immediately; its rows are provisional, and when the finalized zone later
    covers their blocks they are either promoted or — if the reorganized chain never
    confirmed them — deleted with a warning in the log.
- Per-address indexed queries remain for targeted tasks: the initial history of a new
  binding, spot reconciliation.
- Catch-up after downtime is bounded per pass: the native scan by a block cap, the token
  scan by a time window — the cursors advance in bounded steps, so one pass's provider
  traffic and memory stay predictable and progress is committed every pass (while the
  token scan is catching up, the reorg cleanup of token rows is deferred — their
  confirmation is still ahead). A window the provider cannot answer in one call is
  halved and asked again: the price of a window is counted in events, and how many of
  them a window holds is decided by the network, not by the gateway — a time bound
  alone would let a busy network stall the scan for good.
- A cursor marks proven completeness, not a finished code path. It advances only as far
  as the provider's answer actually covered — a short answer is never read as "no
  transfers here" — and it does not advance at all while any owner's rows from that
  pass went unstored: the range is scanned again instead. The alternative loses the
  payment for good, because the native scan has no overlap to bring it back and the
  provisional row of the same transfer would then be cleaned up as a reorganization
  victim.
- Failures are isolated within the pass and logged: a provider "slow down" answer pauses
  that network's scan until the next pass; a failing user database excludes that owner
  for the rest of the pass without stopping the others; a broken numeric/date setting
  degrades to its default with an error log; an unusable entry of the watched-contracts
  setting is dropped on its own, and the sound entries keep being scanned.
- Results are written to each owner's database via the [Storage Layer](storage.md).

## Data Access

All blockchain access goes through the [Chain Abstraction](chain-abstraction.md): public
provider APIs first, a self-hosted node (RPC) later — without changes to watcher logic.

The watcher keeps its service state — the cursors of the authoritative scan per network
(the last finalized block processed and the time cursor of the token scan) — in the
shared registry database: it is the scan resume point after a stop
(see [ADR-0010](../decisions/0010-networks-assets-financial-data.md),
[ADR-0021](../decisions/0021-two-phase-scanning.md)). The preview zone has no cursor —
it is rescanned in full every pass.

## Interfaces

- Module: the single pass (`run_pass`) and the continuous loop over it; the loop is started
  and supervised by the orchestrator.
- Console command `seedrays watch` — one pass; the data directory comes from the gateway
  configuration file (the deployment layer of
  [ADR-0016](../decisions/0016-config-layers.md) and
  [ADR-0026](../decisions/0026-configuration-file.md)).
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
- [ADR-0021: Two-Phase Scanning](../decisions/0021-two-phase-scanning.md)
- [Monitoring](../../40-operations/monitoring.md)
