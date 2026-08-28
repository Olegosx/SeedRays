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

_To be defined._

## Related

- [Architecture Overview](../overview.md)
- [HTTP API](http-api.md)
- [Watcher](watcher.md)
- [ADR-0001: Library-First Core](../decisions/0001-library-first-core.md)
