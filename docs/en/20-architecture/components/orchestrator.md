[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/orchestrator.md)

# Orchestrator

Service layer of the backend: owns the business logic and coordinates the functional components.

## Purpose

- Exposes gateway operations ("issue a payment address", "report incoming payments and
  balances", …) as a clean programmatic interface. The [HTTP API](http-api.md) and the console
  commands are thin adapters over this interface.
- Calls the [Key Generator](key-generator.md), [Derivation Module](derivation.md) and
  [Watcher](watcher.md) as regular Python modules.
- On startup launches the HTTP API server and the watcher in parallel and supervises them:
  a failure of one is logged and the component is restarted without taking down the other.
  See [ADR-0003](../decisions/0003-single-process-supervised.md).

## Interfaces

- Operations module — the business core consumed by the API groups: resolving an API key
  to its application, idempotent binding issue (with derivation-index reuse across
  networks of one wallet), addresses, balances (received + pending), incoming history,
  application users.
- Supervisor — one process (ADR-0003): the API server and the watcher loop run as parallel
  tasks; a crash of either is logged and restarted without taking down the other.
- Console command `seedrays serve` — migrates the databases and runs the gateway; the data
  directory and the bind address come from `SEEDRAYS_DATA_DIR` and `SEEDRAYS_BIND`
  (the bootstrap layer of [ADR-0016](../decisions/0016-config-layers.md)).

## Related

- [Architecture Overview](../overview.md)
- [HTTP API](http-api.md)
- [Watcher](watcher.md)
- [ADR-0001: Library-First Core](../decisions/0001-library-first-core.md)
