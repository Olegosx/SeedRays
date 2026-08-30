[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0015-tron-provider.md)

# ADR-0015: TRON Data Provider — TronGrid; Chain Source Interface

**Status:** accepted; extended by [ADR-0018](0018-range-scanning.md) — range scanning via
the contract-events and block-range endpoints becomes the primary acquisition

## Context

Per network, the watcher needs the current height and the transfers observed on an address
([ADR-0007](0007-address-centric-watcher.md),
[ADR-0010](0010-networks-assets-financial-data.md)). Facts verified against the official
documentation (developers.tron.network, August 2026):

- **TronGrid** is the official hosted node service of the TRON ecosystem, offering both
  standard node calls and pre-indexed account-transfer queries (which a raw node does not
  have — we would otherwise scan every block ourselves).
- Base URLs: mainnet `api.trongrid.io`; testnets Shasta `api.shasta.trongrid.io` and Nile
  `nile.trongrid.io`.
- A free **API key** is created in the TronGrid console and sent in the
  `TRON-PRO-API-KEY` header; keys carry their own security settings (per-key rate limit,
  allowlists of origins/user agents/API methods). Without a key access is strictly
  throttled (30-second blocks, HTTP 403).
- **Exact quotas are deliberately not published**: "the console is the source of truth; do
  not hard-code limits" (historically the free plan announced 15 requests/second plus a
  daily quota). Bursts over the per-second limit return HTTP 429. There is **no
  programmatic API for reading one's quota**.
- Finality: a block is **solidified** once 19 of 27 super representatives build on it,
  about one minute behind the head of the chain.

## Decision

- **Provider: TronGrid.** The implementation calls the documented v1 endpoints — TRC-20
  transfers by account, transactions by account, `getnowblock` — via httpx.
- **The chain source interface** has two operations: `latest_block()` and
  `transfers(address, since, only_confirmed)`, returning normalized transfer events: txid,
  direction, the network-specific asset with symbol and decimals (feeding the asset
  catalog), the amount in minimal units, the block number when the provider reports it,
  the time and the success/failure status.
- **Networks in code: `tron` and `tron-nile`** — one code path differing only by the base
  URL. Which networks are active is the operator's configuration decision; a production
  instance does not enable the test network.
- **Request pacing is configuration, not hard-code** (following the provider's own
  guidance): per-provider rate and daily budget live in the registry settings, the
  operator copies the actual numbers from their TronGrid console; defaults are
  conservative.
- **HTTP 429/403 are classified as a dedicated "slow down" error**, distinct from data
  errors; the waiting/retry policy belongs to the watcher.
- **The confirmation threshold is a per-network setting; TRON default — 19** (matching
  solidification).

## Alternatives Considered

- **TronScan API** (block explorer API): unstable, undocumented contract — not for
  production; rejected.
- **Commercial node providers** (QuickNode, GetBlock, …): paid, raw node access without
  indexed account transfers — more code on our side for money; rejected for the start.
- **A self-hosted node**: the already-planned evolution path behind the same abstraction;
  a dedicated ADR when the time comes.

## Consequences

- The indexed TRC-20 endpoint reports no block number — the event carries it as unknown;
  confirmation of TRC-20 transfers relies on the provider's solidified view
  (`only_confirmed`).
- The API key is a gateway setting in the registry ([ADR-0008](0008-shared-registry-db.md));
  it never appears in code, logs or the repository.
- Tests run on canned response shapes from the documentation; a live check against Nile
  exists as a manually enabled test.

## Related

- [Chain Abstraction](../components/chain-abstraction.md)
- [Watcher](../components/watcher.md)
- [ADR-0007: Address-Centric Watcher Pass](0007-address-centric-watcher.md)
- [ADR-0010: Networks, Assets and Financial Data Structures](0010-networks-assets-financial-data.md)
