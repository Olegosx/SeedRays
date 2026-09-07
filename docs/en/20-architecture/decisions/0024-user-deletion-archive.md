[Index](../../index.md) · [Decision Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0024-user-deletion-archive.md)

# ADR-0024: User Deletion — Two-Step, Through a Server-Side Archive

**Status:** accepted

## Context

The operator needs a way to remove a user from the gateway entirely — e.g. when the
gateway was used for illegal activity. Plain erasure is dangerous: a mistaken
deletion would destroy financial history irreversibly. At the same time the
architecture makes relocation cheap: all of a user's data lives in one directory
([ADR-0005](0005-multi-user-model.md)), and the shared registry
([ADR-0008](0008-shared-registry-db.md)) holds only a few rows (the account,
emails, the xpub and API-key indexes, sessions).

## Decision

- **Deletion is two-step: block first, then delete.** Only a blocked user can be
  deleted — blocking instantly cuts access and kills sessions, and the deletion
  becomes a second, calm step; a mis-click on a list row cannot destroy a live user.
- **Data is not erased — it moves into an archive**: the `archive/` directory inside
  the gateway data directory, one subdirectory per deletion (`<dir>-<datetime>`),
  holding a snapshot of the registry rows (`registry.json` — the account, emails,
  the xpub and API-key index rows) and the user's directory with their database.
  Sessions and password-reset tokens are not archived — a restored user simply
  signs in again.
- The archive deliberately lives **outside** `users/`: databases under `users/` are
  treated as live by the migrations and the watcher; archived databases must not
  be touched.
- **After deletion the login, emails, xpubs and API keys become free** and can be
  taken again.
- **Confirmation is double**: in the panel the operator retypes the login of the
  user being deleted, and the server verifies it once more (`username_mismatch` on
  divergence) — a guard at both the form and the API level.
- **Restoring is a server console command only** (`seedrays user-restore
  --archive …`), not a panel button: the operation is rare and manual. The registry
  rows and the directory move back; the status is restored as it was ("blocked") —
  unblocking stays a separate deliberate operator action. If anything freed by the
  deletion has been taken since (the login, an email, an xpub, a key, the id), the
  command names the conflict honestly and does nothing.
- The deletion is written to the security journal
  ([ADR-0023](0023-security-journal.md)): who deleted whom, into which archive.

## Alternatives Considered

- **Irreversible erasure:** rejected — an operator mistake would destroy financial
  history; unacceptable for a payment system.
- **"Soft" deletion by a database flag:** rejected — the data stays in the working
  set (lists, scans, "live" backups) and the "remove from the gateway" requirement
  is not met; blocking already exists as a separate status.
- **One-step deletion of an active user with a confirmation:** rejected — one
  confirmation is weaker than two actions separated in time; blocking destroys
  nothing and is instantly reversible.
- **A restore button in the panel:** rejected — the operation is rare, inherently
  requires server access (the archive lives on disk) and must not be reachable
  from the web.

## Consequences

- The data directory gains `archive/`; it is part of the backup together with the
  whole data directory.
- Archives are kept indefinitely; pruning old ones is a manual owner decision
  (deliberately no automation).
- SQLite reuses freed ids (the table has no AUTOINCREMENT), so the archive
  subdirectory name gets a numeric suffix on a clash, and restoring also checks
  that the id is free.
- The security journal keeps the deleted user's name and the archive name — the
  trace of the administrative action outlives the account itself.

## Related

- [ADR-0005: Multi-User Model](0005-multi-user-model.md)
- [ADR-0008: Shared Registry Database](0008-shared-registry-db.md)
- [ADR-0023: Security Event Journal](0023-security-journal.md)
- [Operator Panel Scenarios](../../50-frontend/operator-panel.md)
- [Deployment](../../40-operations/deployment.md)
