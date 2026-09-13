[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0003-single-process-supervised.md)

# ADR-0003: Single Backend Process with a Supervising Orchestrator

**Status:** accepted; extended by [ADR-0027](0027-gateway-fee-billing.md) — a third
background task runs under the supervisor: billing

## Context

The backend contains two long-running activities: the HTTP API server and the watcher. They
need a degree of independence: a failure of one should not stop the other. The gateway is
positioned as lightweight, so operational simplicity matters.

## Decision

The backend runs as a single process. On startup the orchestrator launches the API server and
the watcher in parallel (threads or async tasks) and supervises both: a failure is logged and
the failed component is restarted without taking down the other.

## Alternatives Considered

- **Separate processes for API and watcher:** gives true process-level independence, but adds
  inter-process communication and heavier deployment; rejected for the start as contrary to
  the "lightweight" positioning. Kept as the prepared evolution path — the modular structure
  (see [ADR-0001](0001-library-first-core.md)) keeps the future split cheap.

## Consequences

- Failure isolation is at the logic level, not the process level: if the whole process dies,
  both components die with it. A component leaving by way of `sys.exit` — which is how a
  server reports a socket it cannot bind — counts as that component failing, not as the
  gateway failing.
- Anything slow and CPU-bound has to leave the event loop, or it stops the other components.
  Password hashing is the case that matters: one Argon2 check takes about 50 ms, so some
  18 sign-ins a second would occupy the loop completely and the watcher would scan nothing
  meanwhile. Password hashing and the captcha check therefore run in a worker thread.
- Deployment and operations stay simple: one backend process.
- A future split into two processes does not require reworking the components.

## Related

- [Orchestrator](../components/orchestrator.md)
- [Watcher](../components/watcher.md)
- [Deployment](../../40-operations/deployment.md)
