[Index](../index.md) · [Operator Panel Scenarios](operator-panel.md) · [Русская версия](../../ru/50-frontend/user-cabinet.md)

# User Cabinet Scenarios

Scenarios of what a user (wallet owner) does in the personal cabinet and why. This
document is written before implementation and serves as the basis for designing the
user API and the frontend pages.

> Work in progress: scenarios are added as they are agreed upon.

## Account and Sign-In Methods

An account and a sign-in method are different concepts. The account is single: username,
email, status; it is the wallet owner, and the rest of the gateway knows only its
identifier. An account may have several sign-in methods, and all of them are different
paths to the same account:

- **password** — the base method of the first stage (stored as an Argon2 hash on the
  account);
- **external authorization services (OAuth)** — plugged in at later stages without
  reworking the database or the architecture: a separate "external identity" entity — the
  pair "provider + user identifier at the provider", unique and pointing at the account.
  A new provider means new rows of this entity plus credentials in the settings, not
  changes to existing tables.

Every sign-in method ends the same way: the identity is established → the account is
found → a session is issued. The cabinet and the permissions do not depend on how the
user signed in.

An external service is attached from the cabinet by an already signed-in user; detaching
happens there too, with one restriction: the last remaining sign-in method cannot be
removed. The auto-linking policy on the first sign-in through a provider (matching by a
confirmed email) is an open question, to be settled when the external sign-in scenario is
worked out.

## Registration

- Registration is self-service: email + username + password.
- The email and the username are unique within the gateway. The `@` character is forbidden
  in usernames — otherwise a username could collide with someone else's email in the
  shared sign-in field.
- The email is confirmed by a message. Outgoing mail credentials (SMTP) are an operator
  setting in the registry (the settings layer of
  [ADR-0016](../20-architecture/decisions/0016-config-layers.md)).

## Sign-In

- One identifier field + password; the identifier field accepts the username and the email
  interchangeably (lookup by username first, then by email — both are unique, so ambiguity
  is impossible).

## Password Recovery

- Primary path: a reset link sent to the confirmed email.
- Fallback path: a password reset by the operator from the operator panel (the user lost
  access to the email).

## Interface Language

The interface is multilingual: Russian and English at the start; adding a language is one
new dictionary file, with no page rework. The language is resolved
as "the saved choice → the browser language → English"; the switcher lives in the top bar
of every page (the sign-in pages included). The technical design is
[ADR-0019](../20-architecture/decisions/0019-frontend-localization.md) (client-side
dictionaries, one markup for every language).

## Cabinet Structure

The skeleton of every cabinet page: a section sidebar on the left and a top bar.

**The top bar** is present on every page: the language switcher and the user menu (the
name; settings and sign-out inside). The bar's contents will grow — for example, summary
information such as the total balance. On the sign-in pages (before authentication) the
top bar reduces to the language switcher.

The sidebar — five sections:

- **Dashboard** — a summary of the cabinet (contents to be defined separately).
- **Wallets** — the wallet list, adding a wallet by one of the two paths
  ([ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md): submit an xpub
  or generate a key in the gateway), the wallet's addresses.
- **Applications (API)** — application management: creating an application, issuing and
  revoking its API key, configuring the "network → wallet" mapping, viewing the
  application's users and their addresses. An API key belongs to an application, so the
  section is organized by applications.
- **Operation History** — incoming operations across all wallets with filters (wallet,
  network, asset, status).
- **Settings** — the cabinet owner's personal settings: profile (the username — immutable
  after registration; email addresses — a second email can be attached to the account,
  each is confirmed by a message) and security — password change, sign-in methods
  (attaching and detaching external services). There are no gateway-wide settings in the cabinet — those live in the
  operator panel only.

Sign-out is an item of the top bar's user menu.

Addresses and bindings have no section of their own: the application users' addresses are
viewed inside the application, the wallet's addresses inside the wallet.

## Wallets

### Wallet List

The user's wallets. The contents of a list row are to be refined.

### Attaching a Wallet (the Recommended Path)

The "external generation" path of
[ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md): the keys are
created outside the gateway, and only public information is submitted.

- The form: family (TRON, EVM, …), account-level xpub, label.
- The gateway validates the xpub: format correctness and address derivability.
- No secrets are involved on this path; the interface presents it as the recommended one.

### Creating a Wallet in the Gateway (at the User's Own Risk)

The "in-gateway generation" path of ADR-0002; the secret-handling requirements are fixed
in the [key generator](../20-architecture/components/key-generator.md) and the
[threat model](../30-security/threat-model.md): HTTPS only, the secret is never written to
logs, the database or stored responses, memory is wiped after the reveal.

The scenario:

1. The user explicitly chooses the seed phrase length: 12 or 24 words.
2. Families are picked with checkboxes; one phrase creates one wallet per checked family
   (a single phrase yields an xpub of every family).
3. A BIP39 passphrase is optional, behind a checkbox that reveals the input field, with a
   warning next to it: a forgotten passphrase means permanently lost funds.
4. The seed phrase is shown exactly once, to be written down.
5. Write-down confirmation: the user enters three randomly requested words of the phrase
   (e.g. the 3rd, the 7th and the 11th). The wallets are created only after a successful
   confirmation; the gateway stores only the xpubs — one per checked family.

## Related

- [Operator Panel Scenarios](operator-panel.md)
- [ADR-0005: Multi-User Model](../20-architecture/decisions/0005-multi-user-model.md)
- [ADR-0012: Frontend Stack](../20-architecture/decisions/0012-frontend-stack.md)
- [HTTP API](../20-architecture/components/http-api.md)
