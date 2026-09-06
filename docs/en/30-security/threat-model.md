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

Mitigations required from the implementation: HTTPS only (the session cookie is marked
Secure); the secret is never written to logs, the database or stored responses, and no
reference to it is kept beyond the one generating request. A reliable memory wipe is
technically unattainable for Python strings — the residual window until garbage
collection is part of the accepted risk above. The no-persistence guarantee is covered
by a test: the generated phrase appears in the one-time response only, never in the
databases or the log (see [Key Generator](../20-architecture/components/key-generator.md)).

## Related

- [Key Management](key-management.md)
- [Key Generator](../20-architecture/components/key-generator.md)
- [Non-Functional Requirements](../10-requirements/non-functional.md)
