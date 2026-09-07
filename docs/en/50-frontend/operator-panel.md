[Index](../index.md) · [User Cabinet Scenarios](user-cabinet.md) · [Русская версия](../../ru/50-frontend/operator-panel.md)

# Operator Panel Scenarios

Scenarios of what the gateway operator (superadmin) does in the control panel:
managing users and gateway-wide settings.

## The Operator Account and Sign-In

- Operator registration does not exist: the account is created by the console command
  `seedrays operator-create --login …` on the server (the password — by hidden
  interactive input). Whoever has server console access creates operators.
- Sign-in is a separate page behind the same brute-force brake and the same invisible
  proof-of-work captcha ([ADR-0022](../20-architecture/decisions/0022-pow-captcha.md))
  as the cabinet; the panel session lives in its own cookie, separate from
  the user cabinet (the structural boundary of ADR-0004), and lasts 1 day.
- The operator password change lives on the settings page; a change drops the
  operator's other sessions.

## Panel Structure

A sidebar of two sections — "Users" and "Settings"; the top bar is the same as in the
cabinet (the language switcher and the operator menu with sign-out).

## Users

- The gateway user table: name, emails (unconfirmed ones flagged), status
  (active / blocked), wallet count ("—" for an unreadable database), creation date.
- **Blocking** goes through a confirmation dialog: the user and all their applications
  lose access immediately, sessions are terminated. Unblocking is a single action.
- **Password reset** — the fallback recovery path of the cabinet scenario: upon
  confirmation a temporary password is generated and shown exactly once; every session
  of the user is terminated; the password is handed to the user outside the gateway.

## Settings

Forms over the registry `settings` table ([ADR-0016](../20-architecture/decisions/0016-config-layers.md)):

- **TRON provider (TronGrid)**: the API key, the request rate.
- **Mail (Resend, [ADR-0020](../20-architecture/decisions/0020-mail-provider.md))**:
  the API key, the sender address, the gateway base URL for links in emails, the
  development-mode flag (auto-confirm emails without messages).
- **Watcher**: the pass interval, the scan overlap. The watched token contract list
  (`watcher.contracts.<network>`) is not edited by the panel — the owner's decision,
  the setting is entered manually.
- **Network & deployment**: the trusted reverse proxies (`gateway.trusted_proxies` —
  IPs and CIDR ranges, comma-separated; e.g. the local nginx/Apache address, the
  Cloudflare ranges). On connections from a listed proxy the gateway resolves the
  visitor IP from `X-Forwarded-For` — the rate limiter and the security journal then
  see real addresses; empty means "trust 127.0.0.1 only". Applied after a restart.
- **Security journal ([ADR-0023](../20-architecture/decisions/0023-security-journal.md))**:
  the file size before rotation (MB) and the number of compressed archives kept;
  applied after a gateway restart. The journal itself is read on the server
  (`logs/security.log` in the data directory) — the panel has no journal screen.
- Secret values (API keys) are never returned: the form shows only a
  "configured / not configured" flag; an empty secret field on save means "keep".
- Below — the per-network watcher status (read-only): the last processed block and the
  time of the last pass.

## Related

- [User Cabinet Scenarios](user-cabinet.md)
- [ADR-0005: Multi-User Model](../20-architecture/decisions/0005-multi-user-model.md)
- [ADR-0012: Frontend Stack](../20-architecture/decisions/0012-frontend-stack.md)
- [HTTP API](../20-architecture/components/http-api.md)
