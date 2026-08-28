[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0004-two-api-groups.md)

# ADR-0004: Two API Route Groups with Different Access Rights

**Status:** accepted; extended by [ADR-0005](0005-multi-user-model.md) — with the multi-user
model the two groups become three, one per access role (operator, user, application)

## Context

The HTTP API has two kinds of consumers: external applications that access data with their
own key and need new payment addresses issued, and the gateway operator (via the frontend)
who configures the gateway and executes commands. Applications must not be able to configure
the gateway or execute commands.

## Decision

One HTTP server, one core (the orchestrator), two route groups with different access rights:

- **Application API** — authenticated by an application key (API key): reading data
  (balances, incoming payments, statuses) and issuing a new payment address. Configuration
  and command routes do not exist in this group — they are absent as routes, not merely
  forbidden by a permission check.
- **Admin API** — full operator authentication: configuration and commands. Can be bound to
  a separate port or the local interface only, hiding it from the outside.

## Alternatives Considered

- **A single route group with role checks:** rejected — a missed check on any route becomes a
  privilege escalation; physically separate groups make the boundary structural.
- **Two separate API services:** rejected for the start as operationally heavier; the group
  separation already gives the needed boundary within one server.

## Consequences

- The application boundary is structural: nothing to misconfigure per-route.
- The admin surface can be hidden from the network entirely.
- The frontend uses the Admin API (plus data reading where needed); applications use the
  Application API only.

## Related

- [HTTP API](../components/http-api.md)
- [Orchestrator](../components/orchestrator.md)
