[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0014-key-standards.md)

# ADR-0014: Key Generation and Derivation Standards

**Status:** accepted

## Context

The seed phrase a user writes down must open the funds in any standard wallet — hardware or
software — without our code. The online part must derive payment addresses without any
secret (watch-only, [ADR-0002](0002-watch-only-online-part.md)). The implementation library
and its test-vector safety net are fixed by [ADR-0013](0013-backend-stack.md).

## Decision

- **BIP39 mnemonic; the length (12 or 24 words) is chosen explicitly by the user** at
  generation time — the console command has no default.
- **Optional BIP39 passphrase**: entered interactively with hidden input only — never as a
  command argument (process arguments are visible system-wide), never stored, never sent to
  the gateway. The same mnemonic with a different passphrase opens a completely different
  wallet; a forgotten passphrase means permanently lost funds — the user is warned.
- **Derivation paths follow BIP44** with SLIP-0044 coin codes: TRON —
  `m/44'/195'/0'/0/index`; the EVM family — `m/44'/60'/0'/0/index` (Ethereum's code for all
  EVM networks, which is what makes the address identical everywhere — matching the family
  concept of [ADR-0010](0010-networks-assets-financial-data.md)). The account is always 0.
- **The wallet boundary is the account-level xpub** (`m/44'/coin'/0'`): the last hardened
  step is behind it, so the online part derives addresses by two soft steps (`0/index`) and
  can reach neither the secret nor other accounts. This is also exactly the level hardware
  wallets export.
- The `keygen` and `derive` console commands print to the terminal only and never write
  secrets to disk or logs.

## Alternatives Considered

- **Non-standard derivation paths:** rejected — funds would be visible only to our
  software; a user restoring the phrase in a standard wallet would see zero balances.
- **Root-level xpub:** rejected — hardened steps make a public root useless for derivation.
- **Chain-level xpub (`m/.../0'/0`):** works, but non-standard for import into other
  software; no gain over the account level.
- **Handing the account xprv to the server:** rejected — breaks
  [ADR-0002](0002-watch-only-online-part.md).

## Consequences

- Compatibility: the user's phrase opens the funds in Ledger, Trezor, TronLink, MetaMask
  and other standard wallets without our involvement.
- Verified against primary sources, reproduced bit-for-bit by the test suite: the official
  BIP39 vectors (trezor/python-mnemonic), BIP32 test vector 1 (the specification text), and
  the published reference addresses of the standard test mnemonic — EVM (wallet libraries)
  and TRON (the LedgerHQ app-tron README).
- An xpub leak endangers privacy only (all addresses become visible), not funds. The
  dangerous combination "xpub + any child private key" cannot occur in the online part,
  where private keys never exist. To be reflected in the threat model.

## Related

- [Key Generator](../components/key-generator.md)
- [Derivation Module](../components/derivation.md)
- [ADR-0002: Watch-Only Online Part](0002-watch-only-online-part.md)
- [ADR-0013: Backend Stack](0013-backend-stack.md)
