[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0012-frontend-stack.md)

# ADR-0012: Frontend Stack — No-Build Static Files with Vendored Libraries

**Status:** accepted; reconfirmed on 2026-09-03 after re-examining the server-rendering
alternative (Jinja2 + htmx inside the backend) — gateway management through the API as a
product property was the deciding factor

## Context

The frontend is the user cabinet and the operator panel: forms, tables with server-side
pagination, periodic refreshing of figures. The gateway is financial-sector software: the
supply chain of third-party code must be minimal and auditable, and deployments may live in
a closed perimeter without internet access. The boundary "the frontend talks to the backend
only through the HTTP API" is fixed by the architecture.

## Decision

- **Pure static frontend, no build step, no Node.js**: HTML, CSS and modern JavaScript as
  ES modules (natively supported by browsers). What lies in the repository is what runs in
  the browser.
- **Multi-page structure**: each screen is its own HTML file; shared code lives in ES
  modules; no client-side router.
- **API access via the browser's native `fetch`**; the frontend uses only the User API and
  the Operator API.
- **UI kit: Tabler** (Bootstrap 5 underneath) with **Tabler Icons**; client-side
  interactivity: **Alpine.js**. All three are MIT-licensed (Tabler and Tabler Icons
  verified against their repositories, August 2026).
- **Vendoring policy**: every third-party library is a reviewed file with a pinned version
  stored in the repository (`frontend/vendor/`), its license text kept alongside; no CDNs
  in production; updating a library is a deliberate separate task.
- **Amounts stay strings** in JavaScript (the API sends them as strings); any arithmetic —
  BigInt only.

## Security Rationale

- **Supply-chain attacks through package registries are a recurring reality**:
  `event-stream` (2018 — crypto-wallet-stealing code injected into a popular library),
  `ua-parser-js` (2021 — maintainer account takeover), `node-ipc` (2022 — destructive code
  added by the maintainer). A typical bundled frontend pulls hundreds of unauditable
  transitive dependencies; this stack reduces the chain to a handful of reviewed files.
- **A build toolchain is itself software that must be trusted**: npm packages can execute
  arbitrary scripts at install time; bundlers and their plugins execute code on developer
  and CI machines. No build — this whole class of threats does not exist.
- **Auditability**: no transformation between source and delivery; the review surface for a
  security audit is small and readable.
- **Closed-perimeter deployment**: neither a package registry nor build infrastructure is
  needed to deploy — a folder of static files served by anything, including the backend
  process.

## Alternatives Considered

- **SPA on the Node toolchain (Vue/React/Svelte + Vite):** rejected — the dependency tree
  and build infrastructure contradict the supply-chain requirements, with no compensating
  need: the UI is forms and tables.
- **Server-rendered pages inside the backend (Jinja2 + htmx):** rejected — blurs the fixed
  boundary "frontend ↔ HTTP API only".
- **A separate Python frontend service (Jinja2 + htmx + Alpine.js):** workable and
  boundary-preserving, but adds a second service; rejected in favor of even fewer moving
  parts.
- **NiceGUI (UI described in Python):** rejected — deep lock-in to a niche tool with a
  packaged client stack and a websocket channel outside our control.

## Consequences

- Tables, forms and page glue are written by hand; there is no TypeScript — errors surface
  in the browser, so code discipline and tests carry more weight.
- Security updates of vendored libraries become a manual duty: tracking advisories for
  Tabler/Bootstrap/Alpine.js and updating the files is part of the maintenance routine.
- The frontend works offline and in closed perimeters and can be served by any static file
  server.

## Related

- [Architecture Overview](../overview.md)
- [HTTP API](../components/http-api.md)
