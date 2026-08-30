[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0018-range-scanning.md)

# ADR-0018: Range Scanning as the Watcher's Primary Acquisition

**Status:** accepted

## Context

Per-address polling costs O(number of addresses): 3 users × 300 addresses × 2 endpoints =
1800 requests per pass — dead on arrival against TronGrid's free limits (~15 requests/s
plus a daily quota), and no paid plan fixes O(n) scaling. The provider landscape was
verified against the providers' own pages (August 2026): Chainstack — free 3M units/month
at 25 RPS, paid $49–990/month; NOWNodes — tiers from 100k to 100M requests/month;
GetBlock — Pro $399/month, 800 RPS; QuickNode — a 30-day trial, no permanent free tier.
**All alternatives are raw node access with no per-address history at all** — working with
any of them requires range scanning anyway. TronGrid additionally offers indexed endpoints,
including contract events: `/v1/contracts/{contract}/events?event_name=Transfer` over a
time/block range, pages of 200, each event carrying its block number and an unconfirmed
flag (verified against the reference). Native transfers come per block range via
`wallet/getblockbylimitnext`. TRON produces a block every 3 seconds.

## Decision

- **The watcher's primary mechanism is range scanning.** Per pass and per network: fetch
  the head and the finality boundary once; fetch all Transfer events of the active token
  contracts over the range since the cursor; fetch the native-transfer block range; match
  everything **locally** against the binding set. The cost is O(1) in the number of
  addresses — roughly 5–10 requests per minute per network.
- **Per-address indexed queries remain for targeted tasks**: the initial history of a new
  binding, spot reconciliation.
- **Sources are interchangeable** behind the chain abstraction: start on TronGrid (free
  tier plus convenient event endpoints); growth paths — a paid TronGrid plan, another
  provider (raw RPC + the same scanning), or a self-hosted node. A Lite FullNode (no full
  history — sufficient for scanning new blocks) needs ~8–16 cores, 32 GB RAM, ~500 GB
  NVMe; a full-history node starts at 16 cores / 16 GB / ~10 TB disk per the official
  requirements. On a raw node TRC-20 events come from block transaction logs
  (`gettransactioninfobyblocknum`).
- **Privacy gain, recorded deliberately:** range scanning downloads everything and filters
  locally — the provider never learns the gateway's address set, unlike per-address
  polling which effectively publishes it.

## Alternatives Considered

- **Per-address polling as the primary mechanism:** rejected — O(addresses), dies on any
  quota.
- **Multiplying free API keys:** rejected — a fragile fair-use violation, cut off without
  notice.
- **Another provider as "the solution":** rejected as such — they are raw RPC and need the
  same scanning; kept as interchangeable sources.

## Consequences

- The work list of [ADR-0007](0007-address-centric-watcher.md) becomes the local match
  filter: the pass is still address-aware, but acquisition is range-based.
- The chain source interface gains range operations (events of a token contract over a
  range; native transfers of a block range) — implemented with the watcher block.
- The pacing settings of [ADR-0015](0015-tron-provider.md) stay; request budgets become
  independent of the address count.

## Related

- [Watcher](../components/watcher.md)
- [Chain Abstraction](../components/chain-abstraction.md)
- [ADR-0007: Address-Centric Watcher Pass](0007-address-centric-watcher.md)
- [ADR-0015: TRON Data Provider — TronGrid](0015-tron-provider.md)
- [ADR-0017: Universal Transaction Model](0017-universal-tx-model.md)
