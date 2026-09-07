[Index](../index.md) · [Deployment](deployment.md) · [Русская версия](../../ru/40-operations/monitoring.md)

# Monitoring

Observing the running gateway: what to look at, where the journals are, what the
typical problems look like. The gateway is one process, so the primary feed is its
service log plus two structured sources — the security journal and the watcher
status block of the operator panel.

## The Service Log

Under systemd the process log goes to the journal:

```bash
journalctl -u seedrays -f
```

Format: `time LEVEL logger: message`. What matters:

- `gateway starting: api on …` / `trusted reverse proxies: …` — the startup lines
  with the effective bind and proxy trust.
- `WARNING … exited unexpectedly, restarting` / `ERROR … crashed, restarting` —
  a component (API server or watcher) fell and was restarted by the supervisor;
  occasional network-caused watcher restarts are normal, a restart loop is not.
- `ERROR` lines from mail sending or the chain provider — the operation that failed
  is answered to the user explicitly; the log carries the diagnostic context.

## The Security Journal

`logs/security.log` in the data directory
([ADR-0023](../20-architecture/decisions/0023-security-journal.md)): one JSON object
per line — auth events with precise reasons that the API deliberately does not
reveal. Rotated by size, archives gzip-compressed. Reading it:

```bash
tail -f /var/lib/seedrays/logs/security.log
```

```bash
jq -r 'select(.outcome != "success") | [.time, .actor, .event, .outcome, .identifier // "-", .client] | @tsv' \
    /var/lib/seedrays/logs/security.log
```

What to watch for: series of `wrong_password` over one identifier (a targeted
guess), floods of `rate_limited` / `captcha_failed` (a blunt brute force — the
defenses are doing their job), `user_blocked` attempts (a blocked user still trying),
operator events from unexpected addresses.

## The Watcher

The panel's settings page shows the per-network watcher status: the last finalized
block processed and the time of the last pass. A healthy gateway moves the block
cursor and refreshes the pass time on every interval
([ADR-0021](../20-architecture/decisions/0021-two-phase-scanning.md)). A frozen
cursor with a fresh pass time means the provider serves no new finalized blocks — or
rate-limits the gateway (see the service log; the per-second rate is a panel
setting).

## Typical Problems

| Symptom | Where to look | Usual cause |
|---------|---------------|-------------|
| Registration answers `mail_not_configured` | panel settings | No mail credentials, or `gateway.base_url` is empty — a sender without the base URL stays disabled. |
| Emails not delivered, `mail_failed` in answers | service log | The mail provider refused: bad key, or the Resend domain is not verified (delivery then works only to the account owner). |
| All visitors hit the rate limit together | `security.log`: one `client` for everyone | The gateway is behind a proxy that is not listed in `gateway.trusted_proxies`. |
| Deposits not appearing | watcher status, service log | Frozen cursor (provider), missing `watcher.contracts.<network>` list for token deposits, or the wallet/network mapping is not configured for the application. |
| Panel/cabinet sign-in impossible after restore | — | Sessions are server-side; a database restored from backup drops the ones created since. Sign in again. |

## Related

- [Deployment](deployment.md)
- [Watcher](../20-architecture/components/watcher.md)
- [ADR-0023: Security Event Journal](../20-architecture/decisions/0023-security-journal.md)
- [Operator Panel Scenarios](../50-frontend/operator-panel.md)
