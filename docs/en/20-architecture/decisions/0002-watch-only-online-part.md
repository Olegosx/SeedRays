[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0002-watch-only-online-part.md)

# ADR-0002: Watch-Only Online Part; Key Generation Paths

**Status:** accepted

## Context

The gateway accepts incoming payments. The HD wallet standard (BIP32) allows deriving all
payment addresses from the extended public key (xpub) without access to private keys, so the
online part does not need secrets to do its job. At the same time, the gateway may be deployed
locally by its owner, in which case generating keys inside the gateway can be acceptable to
the user.

## Decision

- The online part of the gateway is watch-only: it stores only the xpub, derives addresses
  and observes payments, and cannot sign anything.
- Key generation offers two user-selectable paths (chosen in the frontend):
  1. **External generation (recommended):** the user runs the standalone `keygen` utility (or
     generates keys by any means of their own) and hands the gateway only the xpub.
  2. **In-gateway generation (at the user's own risk):** the gateway generates the seed phrase
     and private key, shows them exactly once for writing down, and stores nothing secret.

## Alternatives Considered

- **Storing the private key/seed on the gateway server:** rejected — a server compromise
  would expose the funds; accepting payments does not require it.
- **External generation only:** rejected — for locally deployed gateways the user should be
  able to choose the simpler path, with the risk made explicit.

## Consequences

- A compromise of the online server does not give the attacker the funds.
- Outgoing operations (payouts), if ever needed, will require a separate, more protected
  signing path — to be designed independently of this decision.
- In-gateway generation carries residual risks, recorded in the
  [Threat Model](../../30-security/threat-model.md), and strict handling requirements,
  recorded in [Key Generator](../components/key-generator.md).

## Related

- [Key Generator](../components/key-generator.md)
- [Derivation Module](../components/derivation.md)
- [Key Management](../../30-security/key-management.md)
