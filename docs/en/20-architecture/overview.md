[Index](../index.md) · [Overview](../00-overview.md) · [Русская версия](../../ru/20-architecture/overview.md)

# Architecture Overview

Top-level structure of the system: backend + frontend.

## Design Approach

The backend is built library-first: all business logic lives in importable modules with clean
interfaces, and every entry point — console commands, the HTTP API — is a thin adapter over
those modules. One codebase serves the standalone commands, the orchestrator and the tests
without duplication. See [ADR-0001](decisions/0001-library-first-core.md).

The gateway is multi-user from day one: every orchestrator operation and every storage call
carries the user context. A user owns one or more wallets; access is split into three roles —
gateway operator (superadmin), user (wallet owner) and the user's applications.
See [ADR-0005](decisions/0005-multi-user-model.md).

## Backend

The core of the backend is the [Orchestrator](components/orchestrator.md) — a service layer
that owns the gateway's business logic and calls the three functional components as regular
Python modules:

- [Key Generator](components/key-generator.md) — generates the wallet seed and keys.
- [Derivation Module](components/derivation.md) — derives payment addresses from the extended
  public key (xpub).
- [Watcher](components/watcher.md) — monitors balances and incoming payments across all
  users' wallets.

The online part of the gateway is watch-only: it holds only the xpub, can derive addresses,
but cannot sign anything. See [ADR-0002](decisions/0002-watch-only-online-part.md).

All blockchain access goes through the [Chain Abstraction](components/chain-abstraction.md)
layer. The external interface is the [HTTP API](components/http-api.md).

The backend stack — Python 3.13, FastAPI + uvicorn, SQLAlchemy Core + Alembic, httpx,
bip-utils, argon2-cffi — is fixed in [ADR-0013](decisions/0013-backend-stack.md).

### Process Model

The backend runs as a single process. On startup the orchestrator launches the HTTP API server
and the watcher in parallel (threads or async tasks) and supervises both: a failure of one is
logged and the component is restarted without taking down the other. Full process-level
independence is a prepared evolution path — the modular structure keeps a future split cheap.
See [ADR-0003](decisions/0003-single-process-supervised.md).

### Data Storage

Each user has their own database and working directory
(see [ADR-0005](decisions/0005-multi-user-model.md)). Alongside them the gateway keeps a
small shared registry database — the user registry, the API-key index, operator accounts and
gateway-wide settings, with no financial data
(see [ADR-0008](decisions/0008-shared-registry-db.md)). All data access goes through the
[Storage Layer](components/storage.md) — an interface at the level of domain operations that
hides the concrete database: SQLite is the first backend, PostgreSQL and MySQL follow.
See [ADR-0006](decisions/0006-storage-abstraction.md). The structure of the stored data is
described in the [Data Model](data-model.md).

### Package Layout

```
backend/
  seedrays/              — Python package (core)
    keygen/              — key generation
    derivation/          — address derivation from xpub
    watcher/             — single pass + continuous loop
    chains/              — chain data source abstraction; implementations: tron/, …
    storage/             — domain-level storage interface; backends: sqlite/, …
    orchestrator/        — service layer, business logic
    api/                 — thin HTTP layer over the orchestrator
    cli.py               — console commands (keygen, derive, watch, …)
  tests/
frontend/                — no-build static files (multi-page HTML + ES modules, vendor/)
```

## Frontend

The frontend provides the user cabinet and the operator interface. It communicates with the
backend only through the [HTTP API](components/http-api.md) and never accesses backend
internals directly.

It is implemented as no-build static files: multi-page HTML with ES modules, the Tabler UI
kit and Alpine.js vendored into the repository, no Node.js — a deliberate supply-chain
decision for financial-sector software. See [ADR-0012](decisions/0012-frontend-stack.md).

## Related

- [Orchestrator](components/orchestrator.md)
- [Key Generator](components/key-generator.md)
- [Derivation Module](components/derivation.md)
- [Watcher](components/watcher.md)
- [Chain Abstraction](components/chain-abstraction.md)
- [Storage Layer](components/storage.md)
- [HTTP API](components/http-api.md)
- [Data Model](data-model.md)
- [Architecture Decision Records](decisions/index.md)
