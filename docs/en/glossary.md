[Index](index.md) · [Overview](00-overview.md) · [Русская версия](../ru/glossary.md)

# Glossary

Terms used across the project documentation.

## Terms

| Term | Definition |
|------|------------|
| ADR | Architecture Decision Record — a short document capturing one architecture decision: context, options considered, choice and consequences. |
| API key | A secret identifier an application presents to access the Application API. |
| Application | An external system connected to the gateway; belongs to a gateway user, is identified by its API key and has its own users. |
| Asset | A network-specific unit of value: a network's native coin, or a token identified by its contract in that network. USDT on TRON and USDT on Ethereum are two different assets. |
| BIP32 | The standard describing hierarchical deterministic (HD) wallets and key derivation. |
| Binding | A permanent mapping "wallet + network + application + application user → payment address (+ memo)" — the gateway's core entity in its primary mode. |
| Chain family | The address and derivation standard a wallet follows (TRON, EVM, …); one EVM wallet yields addresses valid in every EVM network. |
| Derivation | Computing child keys and addresses from a parent key according to BIP32. |
| ES modules | The browser-native JavaScript module system: files import each other directly, no bundler required. |
| HD wallet | Hierarchical deterministic wallet — a wallet whose entire tree of keys and addresses is derived from a single seed. |
| Memo | A tag attached to a transaction in some networks (e.g. TON); it lets many payers share one address while being told apart by the memo. |
| Passphrase | An optional extra secret of BIP39: the same seed phrase with a different passphrase opens a completely different wallet; a forgotten passphrase means permanently lost funds. |
| RPC | The remote-call interface of a blockchain node, used to query chain data directly from a self-hosted node. |
| Schema migration | A controlled change of the database structure (tables, columns) applied when the gateway is upgraded; runs across all user databases. |
| Seed phrase | A human-readable encoding of the wallet seed; whoever knows it controls the funds. |
| Vendoring | Keeping a third-party library as a reviewed, version-pinned file inside the project repository instead of pulling it from a package registry. |
| WAL | Write-ahead logging — an SQLite journal mode in which readers are not blocked by the writer. |
| Watch-only | An operating mode with access to public keys and addresses only: deriving and observing is possible, signing is not. |
| Watcher | The gateway component that monitors balances and incoming payments. |
| Webhook | A callback notification: the gateway itself calls a URL registered by the application when an event occurs (e.g. a new deposit). A planned extension. |
| xpub | Extended public key — allows deriving all wallet addresses without access to private keys. |

## Related

- [Overview](00-overview.md)
