[Index](../../index.md) · [Decision log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0019-frontend-localization.md)

# ADR-0019: Frontend Localization — Client-Side Dictionaries

**Status:** accepted

## Context

The cabinet interface is multilingual: Russian and English at the start, and further
languages must be addable without reworking the pages. The frontend is no-build static files
([ADR-0012](0012-frontend-stack.md)) talking to the backend only through the HTTP API.
The cabinet sits behind sign-in and does not work without JavaScript at all (Alpine.js,
API requests) — so the classic arguments for server-side localization (no-JavaScript
accessibility, search-engine indexing) do not apply to it.

## Decision

- **One markup + translation dictionaries.** Every page label gets a key; translations
  live in dictionaries — plain ES modules (`i18n/ru.js`, `i18n/en.js`), one file per
  language.
- **Language choice**: the user's saved choice → the browser language → English. The
  choice is kept in localStorage; the switcher lives in the top bar of every page.
- **Mechanics**: the `js/i18n.js` module sets the page `lang` attribute and the tab title
  (via the `data-title-key` attribute) and exposes the dictionary to Alpine as the shared
  `$store.i18n` store; labels are injected through `x-text` and attribute bindings; until
  Alpine initializes the page is hidden by an `x-cloak` rule — raw keys never flash.
- **A new language** is one new dictionary file; the markup does not change.

## Considered Alternatives

- **Server-side page rendering per language (PHP or Python):** rejected — it either blurs
  the "frontend ↔ HTTP API only" boundary or adds a second always-running service (and
  PHP would also be a second language stack with its own audit surface); the benefits of
  server-side localization give a signed-in cabinet nothing.
- **Separate pages per language** (the way the documentation is organized): rejected —
  every markup change is multiplied by the number of languages; as pages multiply the
  versions inevitably drift apart.
- **Pre-generating language versions with a build step:** rejected — it brings back the
  build pipeline excluded by [ADR-0012](0012-frontend-stack.md).

## Consequences

- Key discipline: markup carries no literal labels, only dictionary keys.
- Both dictionaries ship to every user — kilobytes at our scale.
- If a public part appears (a landing page, documentation for application developers),
  its localization is decided separately — there the arguments for server-side or
  pre-generated versions are real.

## Related

- [ADR-0012: Frontend Stack](0012-frontend-stack.md)
- [User Cabinet Scenarios](../../50-frontend/user-cabinet.md)
