[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/chain-abstraction.md)

# Chain Abstraction

The "chain data source" interface separating the [Watcher](watcher.md) and the rest of the
backend from concrete blockchain access.

## Purpose

- First implementations use public provider APIs; a later switch to a self-hosted node (RPC)
  must not touch the logic of the components above the abstraction.
- One implementation per chain lives under `chains/` (e.g. `tron/`).

## Supported Chains

TRON first; ETH, TON and others to follow. TRON is served by TronGrid with the network
codes `tron` (mainnet) and `tron-nile` (test network, enabled only by explicit operator
configuration) — see [ADR-0015](../decisions/0015-tron-provider.md).

## Interfaces

The "chain data source" interface exposes two operations:

- `latest_block()` — the current height of the network;
- `transfers(address, since, only_confirmed)` — normalized transfer events on an address:
  txid, direction, the network-specific asset (symbol, decimals — feeding the asset
  catalog), the amount in minimal units, the block number when reported, time and
  success/failure.

Provider "slow down" responses (HTTP 429/403) surface as a dedicated rate-limit error,
distinct from data errors; the waiting policy belongs to the [Watcher](watcher.md).

## Related

- [Architecture Overview](../overview.md)
- [Watcher](watcher.md)
- [ADR-0015: TRON Data Provider — TronGrid](../decisions/0015-tron-provider.md)
