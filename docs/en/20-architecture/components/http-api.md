[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/components/http-api.md)

# HTTP API

Thin adapter over the [Orchestrator](orchestrator.md): routes, input validation, response
codes. No business logic of its own.

## Route Groups

Three groups over the same core, one per access role
(see [ADR-0004](../decisions/0004-two-api-groups.md) and
[ADR-0005](../decisions/0005-multi-user-model.md)):

- **Application API** — for a user's external applications, authenticated by an application
  key (API key). Provides reading data and requesting persistent address bindings for
  application users (see below) — scoped to the wallets of the user who owns the key. No
  configuration or command routes exist in this group.
- **User API (cabinet)** — for the wallet owner, with user authentication. Provides managing
  the user's own wallets (handing over an xpub or in-gateway key generation), the user's
  applications and their keys, the per-application "network → wallet" mapping, and viewing
  the user's own data.
- **Operator API** — for the gateway operator (superadmin), with full operator
  authentication. Provides managing users and gateway-wide settings, including watcher
  control. Can be bound to a separate port or to the local interface only, hiding it from
  the outside.

In every group the boundary is structural: routes of another role's operations do not exist
in the group — they are absent as routes, not merely forbidden by a permission check.

## Application API Operations

Semantic composition (see [ADR-0011](../decisions/0011-application-api-principles.md)):

- **Create address** for an application user — in one network, several networks or all
  networks available to the application. Idempotent: an existing binding is returned as is.
  The first request for an unknown application user creates its record implicitly.
- **Get address(es)** of an application user — read-only; creates nothing.
- **Get balances** of an application user — rows "network + asset", filterable by network
  and asset; each row: total received (confirmed) and the pending amount. The current
  address balance is not exposed: sweeping funds is the owner's technical procedure and
  does not concern the application.
- **Get incoming transaction history** — the same filters plus a status filter (default:
  confirmed; pending and failed on explicit request); paginated.
- **List application users** — paginated.

Every operation is scoped to the caller's **application instance**
(see [ADR-0025](../decisions/0025-application-instances.md)): independent installations of
one application share an API key, and the instance keeps their user identifiers apart.
The listing of application users is scoped too — another installation's users do not exist
for the caller. An application with a single installation names no instance and works
exactly as before.

Applications operate in terms of networks only: the owner configures the application's
"network → wallet" mapping in the cabinet; "all networks" means all networks so configured.

Pagination: a row-count limit parameter, default 10, 0 = everything.

## Conventions

- API version in the path (`/v1/…`).
- JSON everywhere; amounts are strings, never floating-point numbers.
- The API key travels in a request header, never in the URL.
- Unified error format: machine code + human-readable message.

## Planned Extensions

- **Webhook notifications**: the application registers a URL and the gateway calls it on new
  deposits — instead of constant polling. Not in the first version; the API design must not
  preclude it.

## Application API: Implemented Routes

```
POST /v1/app/users/{id}/addresses   [?instance=]  body: {"networks": ["tron"] | "all"}
GET  /v1/app/users/{id}/addresses   [?instance=&network=]
GET  /v1/app/users/{id}/balances    [?instance=&network=&asset=]
GET  /v1/app/users/{id}/history     [?instance=&network=&asset=&status=&limit=]
GET  /v1/app/users                  [?instance=&limit=]
```

- **Authentication**: the application key travels in the `X-API-Key` header. Keys are
  high-entropy random tokens stored as SHA-256 hashes — slow password hashing (Argon2) is
  reserved for human passwords, where it defends against guessing; a random 256-bit token
  cannot be guessed, and a slow hash would only tax every request.
- **Statuses** map to the transaction model of ADR-0017: `confirmed` — applied to the
  balance; `pending` — recorded, not yet applied; `failed` — execution failed.
- The `instance` parameter names the application instance
  ([ADR-0025](../decisions/0025-application-instances.md)); absent or blank means the
  default instance, which is what an application with a single installation always uses.
  It is declared once in the group's caller dependency, so it applies to every route at
  once and cannot be forgotten when a route is added. A shared key means shared trust: the
  instance separates accounting, not security.
- The `asset` filter takes a token contract address or the literal `native`.
- Errors: `{"error": {"code", "message"}}`; validation problems come back the same way.
- The interactive OpenAPI specification is generated by the framework at `/docs`.

## User API: Implemented Routes

```
GET    /v1/user/captcha           (a signed proof-of-work challenge, ADR-0022)
POST   /v1/user/register          body: {"username", "email", "password", "captcha"}
GET    /v1/user/confirm-email     ?token=…   (the link from the email; redirects to sign-in)
POST   /v1/user/login             body: {"identifier", "password", "remember", "captcha"}
POST   /v1/user/logout
GET    /v1/user/me
GET    /v1/user/networks          (networks, families and block-explorer link templates)
GET    /v1/user/wallets
POST   /v1/user/wallets           body: {"family", "xpub", "label"}
POST   /v1/user/wallets/generate  body: {"words", "families", "passphrase"}
GET    /v1/user/applications
POST   /v1/user/applications      body: {"name"} → the key (shown once)
GET    /v1/user/applications/{id}
GET    /v1/user/applications/{id}/users/{id}/addresses  [?instance=]
POST   /v1/user/applications/{id}/users/{id}/addresses  [?instance=]  body: {"networks": ["tron"] | "all"}
POST   /v1/user/applications/{id}/key        (reissue — the new key shown once)
DELETE /v1/user/applications/{id}/key        (revocation)
PUT    /v1/user/applications/{id}/networks   body: {"network", "wallet_id"}
DELETE /v1/user/applications/{id}/networks/{network}
GET    /v1/user/history      [?wallet_id=&network=&asset=&status=&limit=&cursor=]
GET    /v1/user/overview     (counters, receipts by asset, recent operations)
POST   /v1/user/emails       body: {"address"}   (a second email, confirmed by a message)
DELETE /v1/user/emails/{id}                      (the primary one cannot be removed)
POST   /v1/user/password     body: {"current_password", "new_password"}
POST   /v1/user/password-reset          body: {"email", "captcha"}
POST   /v1/user/password-reset/confirm  body: {"token", "new_password"}
```

A password change drops every other session of the user (the current one stays).

Issuing addresses from the cabinet (`POST …/users/{id}/addresses`) is the very same core
call as the identically named [Application API](#application-api-implemented-routes)
route: the gateway user can hand out an
address by hand without standing up an integration. Every property is inherited —
idempotency (a repeat returns what was already issued), the implicit registration of a
previously unseen application user, and the `network_not_configured` error for a network
outside the application's map. The `instance` parameter is the same one the Application
API takes, under the same name and with the same default
([ADR-0025](../decisions/0025-application-instances.md)): the owner issues into a named
installation, and a blank value means the default instance. Reading the application
(`GET /v1/user/applications/{id}`) lists the users of **every** instance and names the
instance of each — the owner needs the whole picture, unlike an application.

The cabinet history pages with "show more" (the owner's decision): the answer carries
`next_cursor` — the opaque position of the last returned row; passing it back as
`cursor` continues the list strictly past it, so new operations appearing on top never
shift or duplicate what is already shown. A missing `next_cursor` means the end.

Password reset: the request answer is always the same — the address's existence is not
revealed; the message goes only to a confirmed email. The token from the message is
one-time (the database keeps its fingerprint, the lifetime is 1 hour, a new request
replaces the previous token), the link leads to the new-password page; a successful
reset drops every session of the user. With no mail configured — `mail_not_configured`
even in the development mode: a reset without a message does not exist. Requests go
through the same brute-force brake.

- **The session** is an HttpOnly, Secure cookie (SameSite=Lax); the database stores the
  token's fingerprint. Mutating requests must carry the `X-CSRF-Token` header issued at
  sign-in and by `/me`; the check is a single structural dependency of every mutating
  route (constant-time comparison), so a new route cannot forget it.
- Registration: the username is 3–64 characters without spaces or `@`; the password is
  8+ characters; the email is lowercased. The confirmation email goes through the mail
  sender ([ADR-0020](../decisions/0020-mail-provider.md)); the confirmation link is built
  from the `gateway.base_url` setting only (the Host header is never trusted), and a
  configured sender without that setting stays disabled. With no mail configured the
  outcome is explicit: registration answers `mail_not_configured` unless the operator
  turned on the development auto-confirm mode (`mail.dev_autoconfirm`).
- Brute-force brake: sign-in and registration are rate-limited by in-process sliding
  windows (per client + identifier for sign-in, per client for registration); over the
  limit the answer is 429 `rate_limited`. The client address behind a trusted reverse
  proxy is resolved from `X-Forwarded-For` (the `gateway.trusted_proxies` setting;
  connections from unlisted addresses keep their socket address — the header cannot
  be spoofed from outside).
- Proof-of-work captcha ([ADR-0022](../decisions/0022-pow-captcha.md)): sign-in,
  registration and the reset request carry a `captcha` field — the solution of a
  challenge from `GET /v1/user/captcha`, solved invisibly by the browser. Challenges
  are HMAC-signed, expire in 10 minutes and are accepted exactly once; the check runs
  after the rate limiter, a failure answers 400 `captcha_failed`.
- Sign-in: one identifier field (username or email); until the primary email is
  confirmed, sign-in is closed (`email_not_confirmed`); "no such user" and "wrong
  password" produce the same `invalid_credentials` answer. The precise reason goes
  to the security journal ([ADR-0023](../decisions/0023-security-journal.md)) —
  the answers stay neutral, the journal does not.
- Applications: the key itself is not stored — the user database keeps its fingerprint
  and the open first characters for identification, the registry keeps the "fingerprint →
  owner" index; the raw key is returned exactly once at creation and reissue; revocation
  clears the fingerprint and drops the index row — the application's access closes
  immediately.
- Wallets: attaching validates the xpub by deriving address zero. An extended PRIVATE
  key is refused with the dedicated `private_key_rejected` code — the user is warned to
  treat it as compromised. An xpub already attached anywhere on the gateway is refused
  with the same neutral `invalid_xpub` answer as a broken key (the response must not
  reveal that the key is in use). In-gateway generation (ADR-0002) is **stateless** —
  the phrase is created in the request's memory and returned exactly once together with
  each family's xpub, nothing is written; after the write-down check the browser attaches
  the wallets through the regular "family + xpub" path, so the seed never travels over
  the network again.
- The static frontend may be served by the gateway process itself (ADR-0012): `/`
  redirects to the sign-in page, the files come from the `frontend/` directory
  (overridable with `frontend_dir` in the configuration file, the deployment layer of
  ADR-0016 and [ADR-0026](../decisions/0026-configuration-file.md)).

## Operator API: Implemented Routes

```
GET  /v1/operator/captcha    (a signed proof-of-work challenge, ADR-0022)
POST /v1/operator/login      body: {"login", "password", "captcha"}
POST /v1/operator/logout
GET  /v1/operator/me
POST /v1/operator/password   body: {"current_password", "new_password"}
GET  /v1/operator/users
POST /v1/operator/users/{id}/status          body: {"status": "active"|"blocked"}
POST /v1/operator/users/{id}/password-reset  → a temporary password (once)
POST /v1/operator/users/{id}/delete          body: {"username"} (the login retyped by the operator)
GET  /v1/operator/settings
PUT  /v1/operator/settings   body: {"values": {key: value}}
GET  /v1/operator/watcher    (per-network watcher cursors, read-only)
```

- The panel session is its own HttpOnly/Secure cookie, separate from the cabinet; CSRF,
  the brute-force brake and the proof-of-work captcha are the same mechanisms as in the
  user group (the panel keeps its own captcha issuer — the group boundary stays
  structural). Operator accounts are created only by the `seedrays operator-create`
  console command.
- Secret settings never appear in answers — only a "set" flag; an empty secret on save
  means "keep". Blocking a user terminates their sessions, and their applications lose
  Application API access immediately.
- User deletion ([ADR-0024](../decisions/0024-user-deletion-archive.md)) works only on
  a blocked user (`user_not_blocked` otherwise); the submitted `username` is checked
  against the login of the user being deleted (`username_mismatch` on divergence).
  The data moves into a server-side archive; restoring is the `seedrays user-restore`
  console command.

## The Gateway Fee: Planned Routes

The billing routes ([ADR-0027](../decisions/0027-gateway-fee-billing.md)) sit in a section
of their own on purpose: the decision is made, the code is not written yet, and mixing them
with the implemented routes above is not an option.

The user group:

```
GET  /v1/user/billing            (invoices, details of the unpaid one, payment network)
PUT  /v1/user/billing/network    body: {"network"}
```

The operator group:

```
GET    /v1/operator/billing/invoices                    [?status=&user_id=]
POST   /v1/operator/billing/invoices/{id}/confirm       body: {"reason"}
GET    /v1/operator/billing/wallets
PUT    /v1/operator/billing/wallets                     body: {"network", "xpub"}
DELETE /v1/operator/billing/wallets/{network}
```

- **Suspension for non-payment is already in force** (unlike the routes above) — an access
  state of its own, independent of the account status: an overdue invoice closes the
  Application API entirely (refused 403 with the `billing_suspended` code) and every cabinet
  route except billing, reading `/v1/user/me` and signing out — otherwise there would be no
  way to pay. Access opens by itself as soon as the invoice is credited.
- **Changing the payment network** is refused while an invoice is unpaid: the details of an
  issued invoice are immutable. Networks without the owner's master wallet are not offered.
- **Manual payment confirmation** requires a reason, lands in the security journal and has
  the same result as a credited payment.
- The fee settings (rate, threshold, term, turnover assets, underpayment tolerance,
  notifications) get no routes of their own — they live in the operator's general set of
  settings, like everything else they manage while the gateway runs.
- Amounts travel as strings, as everywhere; an invoice is denominated in USDT and paid with
  a stablecoin of the chosen network.

## Detailed Specifications

_Request and response schemas are refined as the groups evolve._

## Related

- [Architecture Overview](../overview.md)
- [Orchestrator](orchestrator.md)
- [Data Model](../data-model.md)
- [Functional Requirements](../../10-requirements/functional.md)
- [ADR-0011: Application API Principles](../decisions/0011-application-api-principles.md)
- [ADR-0027: The Gateway Owner's Fee](../decisions/0027-gateway-fee-billing.md)
