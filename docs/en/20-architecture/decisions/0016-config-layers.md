[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0016-config-layers.md)

# ADR-0016: Configuration Layers

**Status:** accepted

## Context

The gateway has configuration of two different natures: what a process needs before it can
open any database, and what the operator manages while the gateway runs (including data
provider API keys). One storage for both does not fit: connection parameters cannot live
inside the database they open, and operator-managed values must not require shell access
and restarts.

## Decision

- **Environment variables — deployment-level bootstrap only**: the gateway data directory
  path, the API bind address/port, and (when PostgreSQL arrives) the registry database
  connection string. Changing these is a deployment action; a restart is expected.
- **The registry `settings` table — everything the operator manages at runtime**
  (extends [ADR-0008](0008-shared-registry-db.md)): data provider API keys (TronGrid and
  future ones), per-provider request rate and daily budget, per-network confirmation
  thresholds, watcher intervals, enabled networks. Managed through the operator panel:
  entering, replacing or revoking a provider key requires no server console and no
  restart.
- Secrets never appear in code, logs or the repository, in either layer.

## Alternatives Considered

- **Everything in environment variables:** rejected — operator-managed values (provider
  keys above all) would require server shell access and restarts to change or rotate.
- **Everything in the database:** rejected — connection parameters cannot live inside the
  database they open; deployment-level values do not belong to the operator's runtime
  domain.

## Consequences

- The registry backup contains provider keys — one more reason registry backups are
  handled as sensitive (they already hold password hashes); provider keys are revocable in
  one click at the provider's console.
- The future operator API gains settings operations; the config module of the backend
  reads the environment layer once at startup.

## Related

- [Storage Layer](../components/storage.md)
- [ADR-0008: Shared Registry Database](0008-shared-registry-db.md)
- [ADR-0015: TRON Data Provider — TronGrid](0015-tron-provider.md)
