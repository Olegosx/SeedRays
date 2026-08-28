[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0001-library-first-core.md)

# ADR-0001: Library-First Core with Thin Adapters

**Status:** accepted

## Context

The three gateway functions (key generation, derivation, watching) must be usable in several
ways: standalone from the terminal, inside the orchestrator, and in tests. Implementing each
entry point separately would duplicate logic.

## Decision

All business logic lives in importable Python modules with clean interfaces. Every entry
point — console commands, the HTTP API server — is a thin adapter that only translates its
transport (CLI arguments, HTTP requests) into calls of those modules.

## Alternatives Considered

- **Logic inside the entry points** (commands and HTTP handlers own their logic): rejected —
  leads to duplicated and diverging behavior between the CLI and the API, and makes the core
  untestable without the transport.

## Consequences

- One codebase serves the CLI, the orchestrator and the tests.
- The core is testable without HTTP or a terminal.
- New interfaces (e.g. another transport) can be added without touching the logic.

## Related

- [Architecture Overview](../overview.md)
- [Orchestrator](../components/orchestrator.md)
