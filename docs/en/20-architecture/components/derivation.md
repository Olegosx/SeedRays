[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/derivation.md)

# Derivation Module

Derives payment addresses from the extended public key (xpub) per the HD wallet standard
(BIP32).

## Purpose

- Runs in the online part in watch-only mode: address derivation requires no private key,
  so a compromise of the server does not expose funds.
  See [ADR-0002](../decisions/0002-watch-only-online-part.md).
- Available both as a module (called by the [Orchestrator](orchestrator.md) to issue payment
  addresses) and as a standalone console command
  (see [ADR-0001](../decisions/0001-library-first-core.md)).

## Interfaces

_To be defined._

## Related

- [Architecture Overview](../overview.md)
- [Key Generator](key-generator.md)
- [Orchestrator](orchestrator.md)
- [Key Management](../../30-security/key-management.md)
