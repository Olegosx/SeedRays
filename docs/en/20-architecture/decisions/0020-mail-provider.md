[Index](../../index.md) · [Decision log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0020-mail-provider.md)

# ADR-0020: Outgoing Mail — a Third-Party Service Behind an Abstraction

**Status:** accepted

## Context

The cabinet needs service emails: registration confirmation, password recovery. Running
an own mail server is a separate operational discipline (domain reputation, spam
folders) out of proportion to a volume of single emails. The gateway's target audience
is not Russian. Prices checked against the services' sites (2026-09-03): Resend — 3,000
emails/month free indefinitely (up to 100/day); Postmark — only 100 emails/month free;
Unisender Go — 6,000/month free for the first two months only, then from 800 RUB/month.

## Decision

- **Emails go through a third-party service**; the first provider is **Resend**
  (`POST https://api.resend.com/emails`, the key in the Authorization header) via httpx.
- **The "mail sender" abstraction** — the same principle as the chain data source
  ([ADR-0015](0015-tron-provider.md)): an interface with a single "send a message"
  operation; the calling code never knows what is behind it. Switching or adding a
  provider (plain SMTP included) is a new implementation, not a rework.
- **Credentials are registry settings** ([ADR-0016](0016-config-layers.md)): the API key
  and the sender address; operator-managed, never in the code or the logs.
- **Development mode**: while the mail credentials are not set, registration confirms
  the email automatically with a logged warning — the gateway works without the external
  service.

## Considered Alternatives

- **An own SMTP server:** rejected — reputation and deliverability cost more than our
  entire email volume; remains a possible implementation behind the abstraction for
  air-gapped installations.
- **Postmark:** the free tier (100 emails/month) is too small; rejected as the start.
- **Unisender Go:** a strong option for a Russian audience (ruble billing, local
  infrastructure), but the gateway's audience is not Russian; rejected as the start.

## Consequences

- Confirmation emails carry a one-time token; the database stores only its SHA-256
  fingerprint with an expiry.
- Delivery depends on an external service; its failure does not fail registrations
  silently — the send error is returned to the user explicitly.
- The future operator settings block gets the mail credential fields.

## Related

- [ADR-0016: Configuration Layers](0016-config-layers.md)
- [User Cabinet Scenarios](../../50-frontend/user-cabinet.md)
- [HTTP API](../components/http-api.md)
