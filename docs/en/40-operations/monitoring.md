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
  a component (the API server, the watcher or billing) fell and was restarted by
  the supervisor; occasional network-caused restarts are normal, a restart loop
  is not.
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

The panel's settings page shows the per-network watcher status: the last finalized block
processed and the token scan cursor. **Both are cursors, not signs of life.** The token
cursor deliberately sits in the past while the scan is catching up after downtime
([ADR-0021](../20-architecture/decisions/0021-two-phase-scanning.md)), so an old value
there does not mean the passes stopped — the catch-up window advances it in bounded steps.

Whether the passes are running at all is answered by the service log: every pass writes a
summary line per network with the bounds it covered (`blocks=`, `tokens=`). A block cursor
that does not move while those lines keep coming means the provider serves no new finalized
blocks — or rate-limits the gateway (the per-second rate is a panel setting). No summary
lines at all means the watcher itself is not running.

## Billing

The owner's fee ([ADR-0027](../20-architecture/decisions/0027-gateway-fee-billing.md))
runs in hourly passes and, like the watcher, writes a summary line per pass:

```
billing pass done: checked=… payments=… foreign=… billed=… issued=… overdue=… no_wallet=…
```

- `checked` / `payments` — how many invoice addresses were polled (only debtors
  are polled) and how many new payments were stored; every payment and every
  issued invoice also gets a line of its own with the amount, the login and the
  transaction.
- `foreign` above zero — an asset outside the `billing.payment_assets.<network>`
  list arrived on an invoice address: it is stored but valued at zero and does not
  move the balance; the owner is emailed.
- `no_wallet` — the logins that have nowhere to be invoiced: their payment network
  has no master wallet.
- With the fee switched off the pass says so explicitly —
  `the gateway fee is switched off: no invoices are issued` — and writes no summary.

Suspending and restoring access is visible twice: as log lines (`user … suspended` /
`user … restored`) and as `billing_suspend` / `billing_restore` events in the
security journal. A failed notification email does not cancel the operation — it
remains an `ERROR billing mail … failed` line.

## Tracing One Payment

A user says their payment never arrived. The service log answers it without the server
console and without opening any database, because every pass writes what it actually did:

- a line per **newly stored** transfer — network, whether it is provisional or finalized,
  direction, amount, asset, the owner's login, transaction id, block and address;
- a line per transfer **applied to the balance**, with the same identifiers;
- the pass summary naming the **bounds actually covered**: the block range (`blocks=`) and
  the token time window (`tokens=`).

Search the log for the transaction id. Three outcomes, and they are fixed differently:

1. **A "stored" line, then an "applied" line.** The gateway has the payment; look at the
   cabinet filters and at the asset — if it is a token outside `watcher.contracts.<network>`,
   the cabinet shows it but the fee turnover does not count it.
2. **A "stored" line without an "applied" one.** The row is provisional: the finalized zone
   has not reached its block yet. Compare the block with `blocks=` of later passes.
3. **No line at all.** Check whether the block is inside a covered range. Below the covered
   bound — the provider did not report the transfer, so a targeted reconciliation is needed;
   above it — the scan has not got there yet, and the cursor bounds tell how far behind it
   is. This is exactly the difference the bounds exist for.

## Typical Problems

| Symptom | Where to look | Usual cause |
|---------|---------------|-------------|
| Registration answers `mail_not_configured` | panel settings | No mail credentials, or `gateway.base_url` is empty — a sender without the base URL stays disabled. |
| Emails not delivered, `mail_failed` in answers | service log | The mail provider refused: bad key, or the Resend domain is not verified (delivery then works only to the account owner). |
| All visitors hit the rate limit together | `security.log`: one `client` for everyone | The gateway is behind a proxy that is not listed in `gateway.trusted_proxies`. |
| Deposits not appearing | watcher status, service log | Frozen cursor (provider), missing `watcher.contracts.<network>` list for token deposits, or the wallet/network mapping is not configured for the application. For one specific payment see [Tracing One Payment](#tracing-one-payment). |
| The cursor does not move while passes keep running | service log | The pass could not store some owner's rows and left the cursor where it was on purpose, so the range is scanned again — the log names the owner. Fix that database and the cursor resumes. |
| A row is missing from the history and the totals are short | service log: `absent from the registry catalog` | The registry catalog and a user's database were restored from copies made at different times. The rows are intact; the asset description is what is missing. |
| An invoice was paid but the balance does not grow | billing summary: `foreign=` | The payment token is not listed in `billing.payment_assets.<network>` — the transfer is stored as a foreign asset valued at zero and is never revalued retroactively. Add the token to the list and credit this payment by confirming the invoice manually in the panel. |
| No invoices are issued despite the turnover | service log | The fee is switched off (`billing.enabled`), the turnover is below the threshold, the turnover assets are not listed in `billing.assets.<network>` — or the user's payment network has no master wallet (`no_wallet=` in the summary names the login). |
| Panel/cabinet sign-in impossible after restore | — | Sessions are server-side; a database restored from backup drops the ones created since. Sign in again. |

## Related

- [Deployment](deployment.md)
- [Watcher](../20-architecture/components/watcher.md)
- [ADR-0023: Security Event Journal](../20-architecture/decisions/0023-security-journal.md)
- [Operator Panel Scenarios](../50-frontend/operator-panel.md)
