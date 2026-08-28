[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0011-application-api-principles.md)

# ADR-0011: Application API Principles

**Status:** accepted

## Context

The Application API serves the primary mode ([ADR-0009](0009-address-bindings-primary-mode.md)):
applications request addresses for their users and read what has arrived. Applications must
not learn about the owner's internal entities, and sweeping funds off addresses is the
owner's technical procedure that does not concern applications.

## Decision

Operations (semantic level; exact request/response formats are a separate step):

1. **Create address** for an application user — in one network, several networks or all
   networks available to the application. Idempotent: if the binding already exists, the
   existing one is returned. The first request for an unknown application user creates its
   record implicitly — there is no separate "register user" operation.
2. **Get address(es)** of an application user — the read-only counterpart; creates nothing.
3. **Get balances** of an application user — rows "network + asset", filterable by network
   and asset. Each row carries: total received (confirmed) and the pending amount. The
   current address balance is not exposed to applications.
4. **Get incoming transaction history** — the same filters plus a status filter (default:
   confirmed; pending and failed on explicit request); paginated.
5. **List application users** — paginated.

Principles:

- **Applications operate in terms of networks only.** The owner configures the
  application's "network → wallet" mapping in the cabinet; "all networks" means all
  networks so configured. Applications never see wallets.
- **Pagination**: a row-count limit parameter, default 10, 0 = everything.
- **Conventions**: API version in the path (`/v1/…`); JSON everywhere; amounts are strings,
  never floating-point numbers; the API key travels in a request header, never in the URL;
  a unified error format (machine code + human-readable message).
- **Webhook notifications are a planned extension** (the gateway calls a URL registered by
  the application on new deposits, instead of constant polling). Not in the first version;
  the API design must not preclude it.

## Alternatives Considered

- **Wallet specified in the application's request:** rejected — leaks the owner's internal
  entities to applications and complicates keys ([see the mapping principle]).
- **Exposing the current address balance to applications:** rejected — sweeping is the
  owner's procedure; applications need what arrived (confirmed and pending), not what is
  currently lying on the address.
- **A separate "register user" operation:** rejected — implicit creation on the first
  address request means fewer states and fewer errors.

## Consequences

- The User API (cabinet) must include managing the per-application "network → wallet"
  mapping.
- The data model gains the mapping as part of the application entity.
- Webhooks enter the roadmap as an extension point.

## Related

- [HTTP API](../components/http-api.md)
- [Data Model](../data-model.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](0009-address-bindings-primary-mode.md)
