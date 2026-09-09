[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0025-application-instances.md)

# ADR-0025: Application Instances — the Namespace of Application User Ids

**Status:** accepted; refines [ADR-0009](0009-address-bindings-primary-mode.md) (the identity
of an application user) and [ADR-0011](0011-application-api-principles.md) (the shape of the
Application API operations)

## Context

One application can be deployed as several independent installations: a shop engine handed to
several owners, or a production and a staging environment of the same integration. Such
installations are naturally configured alike and connect to the gateway with one shared API
key.

The application-user identifier is unique within the application
([ADR-0009](0009-address-bindings-primary-mode.md)), so "user 42" of one installation and
"user 42" of another resolve to the same application user. The gateway hands them one payment
address; two payers' funds land on one address and one balance with nothing left to tell them
apart. For a payment gateway that is the worst class of error.

Several copies of a *single* deployment behind a load balancer are a different situation and
need nothing: they share one database, so the same identifier means the same person, and the
idempotent address issuance already returns one address to every copy.

## Decision

- The identity of an application user becomes the triple **application + instance + external
  id**. The instance is an opaque string chosen by the caller, which the gateway does not
  interpret — exactly like the external id itself.
- **The dimension lives on the application user, not on the binding.** Bindings reference the
  application-user row by its internal id, so binding uniqueness, address uniqueness and the
  reuse of the derivation index across networks all keep installations apart without a single
  change of their own.
- **The default instance is the empty string, not a literal such as `main`.** This is the
  idiom the schema already uses for `bindings.memo` and `assets.contract_address`: an empty
  string instead of NULL, because NULL does not equal NULL in SQL and uniqueness with it does
  not work. An application with one installation *is* the empty instance — not a special case
  in code, and existing rows stay correct after the migration with no data change and no
  reissued addresses.
- **The value travels as the `instance` query parameter.** In the Application API it is
  declared once in the caller dependency, so it reaches the whole route group at once and
  cannot be forgotten when a route is added — no route of that group exists without that
  dependency. The cabinet routes take the same parameter under the same name with the same
  default. Absent or blank means the default instance.
- **The Application API is always scoped to the caller's own instance**, the user list
  included: another installation's users do not exist for it, rather than being "forbidden".
  The cabinet shows the owner every instance at once, with the instance named explicitly.
- **A shared key means shared trust.** The instance separates accounting, not security:
  installations that share a key can name each other's instance. Where a real boundary is
  required, the road is a key per installation (see the alternatives).

## Alternatives Considered

- **A separate application per installation:** works today with no changes at all, but
  duplicates the "network → wallet" mapping in every application and fills the cabinet with
  near-identical entries. Rejected as the default; it stays the right answer when
  installations belong to different owners and must not see each other.
- **The instance on the binding:** rejected — the collision lives in the identifier
  namespace, which belongs to the application user. On the binding it would have to enter
  three uniqueness constraints, would need keeping in agreement with the application-user
  row, and would still leave two different payers sharing one application-user row and one
  line in the cabinet.
- **A literal `main` as the default:** rejected — `main` remains a value an application may
  send explicitly, so the empty string and `main` would be two namespaces that look the same.
  Preventing that would mean reserving the word and normalizing input — machinery bought for
  a name.
- **The instance in a request header:** rejected — the gateway's headers carry authentication
  (`X-API-Key`) and CSRF protection, and a business dimension among them breaks that simple
  rule. A query parameter shows up in the generated OpenAPI specification, goes through the
  ordinary input validation and keeps the cabinet and the Application API symmetric. The
  structural guarantee "cannot be forgotten" comes from the shared dependency, not from the
  transport.
- **The instance in the path** (`/v1/app/instances/{instance}/users/{id}/…`): expresses the
  identity most fully, but an empty path segment is not addressable, so the default instance
  would need a placeholder segment or a second route shape — and every existing integration
  URL would change.
- **A key per instance inside one application:** the strongest option on security — the
  instance comes from the key, cannot be spoofed, and access is revoked per installation
  while the network mapping stays shared. Deferred, not rejected: it needs the application key
  moved out of the application row into a table of its own. The data model above does not
  change when that day comes, so the switch costs no data migration and no reissued
  addresses.

## Consequences

- Existing integrations are untouched: they are the default instance, and their addresses do
  not change.
- Every new instance consumes fresh derivation indexes of the same wallet, so a wallet's
  address counter grows faster. This is correct — different payers need different addresses.
- The instance name travels in the request URL and therefore reaches reverse-proxy logs. Name
  instances neutrally (`shop-2`), not after the client.
- The cabinet's application page lists the users of every instance and names the instance in
  its own column; the user counter of an application stays per application.
- Downgrading the schema is only possible while a single instance exists: restoring the old
  constraint fails once two installations have registered the same identifier — and that is
  the right outcome, because the alternative is silently discarding someone's bindings.

## Related

- [Data Model](../data-model.md)
- [HTTP API](../components/http-api.md)
- [ADR-0009: Persistent Address Bindings as the Primary Mode](0009-address-bindings-primary-mode.md)
- [ADR-0011: Application API Principles](0011-application-api-principles.md)
- [User Cabinet Scenarios](../../50-frontend/user-cabinet.md)
