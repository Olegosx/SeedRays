[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/chain-abstraction.md)

# Chain Abstraction

The "chain data source" interface separating the [Watcher](watcher.md) and the rest of the
backend from concrete blockchain access.

## Purpose

- First implementations use public provider APIs; a later switch to a self-hosted node (RPC)
  must not touch the logic of the components above the abstraction.
- One implementation per chain lives under `chains/` (e.g. `tron/`).

## Supported Chains

TRON first; ETH, TON and others to follow. Details to be defined.

## Interfaces

_To be defined._

## Related

- [Architecture Overview](../overview.md)
- [Watcher](watcher.md)
