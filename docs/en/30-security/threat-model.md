[Index](../index.md) · [Key Management](key-management.md) · [Русская версия](../../ru/30-security/threat-model.md)

# Threat Model

What the system defends against. Content to be extended.

## Threats

_To be defined._

## In-Gateway Key Generation (Accepted Residual Risks)

When the user chooses in-gateway generation
(see [ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md)), the following
risks exist and are accepted by the user explicitly:

- At the moment of generation the secret (seed phrase, private key) exists in backend memory.
- The secret travels over the network to the user's browser for the one-time display.

Mitigations required from the implementation: HTTPS only; the secret is never written to logs,
the database or stored responses; memory is cleared right after the display; the guarantees
are covered by tests (see [Key Generator](../20-architecture/components/key-generator.md)).

## Related

- [Key Management](key-management.md)
- [Key Generator](../20-architecture/components/key-generator.md)
- [Non-Functional Requirements](../10-requirements/non-functional.md)
