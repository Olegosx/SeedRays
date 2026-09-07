[Index](../../index.md) · [Decision log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0023-security-journal.md)

# ADR-0023: Security Event Journal — Precise Inside, Neutral Outside

**Status:** accepted

## Context

The auth endpoints deliberately answer with neutral errors: "no such user" and "wrong
password" are the same `invalid_credentials`, a password-reset request never reveals
whether the address exists. Right for the attacker-facing side — but the operator
investigating an incident is left with the same blur. The gateway needs a journal that
records what actually happened. A database table was considered and rejected: under a
brute-force flood every refused attempt would insert a row (~2.5 GB/day at a modest
100 rps), and each insert would contend for the SQLite write lock against the financial
writes of the watcher.

## Decision

- **Precise inside, neutral outside**: journal entries carry the true reason —
  `wrong_password`, `unknown_identifier`, `user_blocked`, `unknown_email`,
  `unconfirmed_email`, … — while the API answers stay exactly as they were. Reasons are
  recorded at the point where they are still known: inside the sign-in and reset
  operations, before neutralization; everything else at the route level.
- **A rotated file, not the database**: JSON Lines in `logs/security.log` under the
  gateway data directory. One event — one JSON object: time, actor (user/operator),
  event, outcome, the identifier as submitted, resolved user/operator id, client
  address, event-specific details. Rotation by size with gzip-compressed archives
  (the standard-library `logging` machinery, no new dependencies).
- **Rotation is operator-configurable**: registry settings `seclog.rotate_mb` (file
  size before rotation, default 100 MB) and `seclog.backups` (compressed archives
  kept, default 10) on the panel's settings page; applied on gateway restart. The
  worst-case disk ceiling is `rotate_mb × (backups + 1)`; gzip typically shrinks the
  archives an order of magnitude below it.
- **What is never journaled**: passwords, session and reset tokens, captcha payloads,
  setting values (only the **keys** of changed settings are recorded). A journal
  failure never fails the operation itself — authentication availability comes first;
  the breakage is reported once in the process log.
- **Coverage**: user sign-in, registration, password reset (request and confirmation),
  password change, captcha and rate-limit refusals; operator sign-in and password
  change; operator administrative actions — user block/unblock, user password reset,
  settings updates.

## Considered Alternatives

- **A registry table**: convenient querying and a future panel screen, but unbounded
  growth under attack and write-lock contention on the auth hot path; rejected.
- **The ordinary process log**: entangles security events with operational noise and
  has no per-event structure; rejected as the primary store (operational lines stay).

## Consequences

- The journal contains sensitive metadata (who signed in when and from where). An
  accepted cost of precision: an identifier mistyped into the login field — including
  a password entered there by mistake — lands in the journal verbatim. The file lives
  only in the gateway data directory, readable by whoever runs the server.
- Behind a reverse proxy the client column shows the proxy address until the operator
  lists the trusted intermediaries in the `gateway.trusted_proxies` setting (IPs and
  CIDR ranges; applied on restart): the server then takes the visitor address from
  `X-Forwarded-For` — but only on connections arriving from a listed proxy, so the
  header cannot be spoofed from outside. The rate limiter shares the same resolution.
- Rotation compresses synchronously: the single process pauses for a second or two
  when a full file rotates — under attack roughly once an hour, otherwise rare.
- A panel screen for reading the journal is deliberately deferred: the operator of a
  self-hosted gateway reads the file directly (`tail`, `jq`).

## Related

- [ADR-0003: Single Backend Process with a Supervising Orchestrator](0003-single-process-supervised.md)
- [ADR-0016: Configuration Layers](0016-config-layers.md)
- [ADR-0022: Anonymous Auth Endpoints Behind a Self-Hosted Proof-of-Work Captcha](0022-pow-captcha.md)
- [HTTP API](../components/http-api.md)
- [Operator Panel Scenarios](../../50-frontend/operator-panel.md)
