[Index](index.md) · [Русская версия](../ru/00-overview.md)

# Overview

SeedRays is a lightweight payment gateway for accepting cryptocurrency payments into an HD wallet.

## At a Glance

- Core components: key generator, derivation module, watcher (balances, incoming payments) —
  coordinated by the orchestrator (the service layer of the backend).
- The online part is watch-only: it holds only the extended public key (xpub) and cannot sign.
- Multi-user: each user has a personal cabinet, owns one or more wallets, and gets their own
  database and working directory. Three access roles: gateway operator, user, user's
  applications.
- Primary mode: persistent binding of payment addresses to accounts of connected
  applications; single invoices come later.
- Supported chains: TRON first; ETH, TON and others to follow.
- Backend: Python; blockchain data sources sit behind an abstraction layer; data lives in
  per-user databases plus a small shared registry, behind the storage layer (SQLite first,
  PostgreSQL and MySQL later).
- External interface: a small HTTP API with three route groups — one per access role.
- Top-level structure: backend + frontend (no-build static files, no Node.js).

## Scope

_To be defined._

## Related

- [Functional Requirements](10-requirements/functional.md)
- [Architecture Overview](20-architecture/overview.md)
- [Glossary](glossary.md)
