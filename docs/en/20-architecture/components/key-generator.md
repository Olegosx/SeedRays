[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/key-generator.md)

# Key Generator

Generates the wallet seed and keys. One module powers both the standalone console utility and
in-gateway generation (see [ADR-0001](../decisions/0001-library-first-core.md)).

## Setup Paths

The user chooses one of two paths, offered in the frontend:

1. **External generation (recommended).** The user downloads the utility (the `keygen` console
   command) or generates the keys by any means of their own, and hands the gateway only public
   information — the xpub. Secrets never touch the gateway.
2. **In-gateway generation (at the user's own risk).** The gateway generates the seed phrase
   and the private key, shows them to the user exactly once for writing down, and stores
   nothing secret — it keeps only the xpub.

See [ADR-0002](../decisions/0002-watch-only-online-part.md).

## Requirements for In-Gateway Generation

- HTTPS only; the secret must never be written to logs, the database or any stored API
  responses.
- Memory holding the secret is cleared right after the one-time display.
- The "shown once, never stored" promise must be verifiable in code and covered by tests.
- The residual risks are recorded in the [Threat Model](../../30-security/threat-model.md).

## Interfaces

- Module: mnemonic generation (12 or 24 words — the user's explicit choice), BIP39
  validation, account-level xpub per family, optionally with a BIP39 passphrase.
- Console command `seedrays keygen --words 12|24 [--passphrase] [--family …]` — prints the
  phrase (shown once, stored nowhere) and the account xpubs; writes nothing to disk or
  logs. Standards are fixed in [ADR-0014](../decisions/0014-key-standards.md).

## Related

- [Architecture Overview](../overview.md)
- [Derivation Module](derivation.md)
- [Key Management](../../30-security/key-management.md)
- [Threat Model](../../30-security/threat-model.md)
- [ADR-0014: Key Generation and Derivation Standards](../decisions/0014-key-standards.md)
