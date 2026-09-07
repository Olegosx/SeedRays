[Index](../../index.md) · [Decision log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0022-pow-captcha.md)

# ADR-0022: Anonymous Auth Endpoints Behind a Self-Hosted Proof-of-Work Captcha (ALTCHA)

**Status:** accepted

## Context

The anonymous authentication endpoints (sign-in, registration, password-reset request,
operator sign-in) are guarded only by the in-process rate limiter. That brake caps the
frequency of attempts but leaves each attempt free; a distributed attacker with many
addresses walks around per-client windows. A captcha adds a per-attempt cost. External
captcha services (reCAPTCHA, hCaptcha, Turnstile, and the second tier — GeeTest,
MTCaptcha, Friendly Captcha, CaptchaFox) all require per-installation registration and a
third-party dependency, which contradicts the gateway's self-installable nature; the
survey of the market was done 2026-09-07.

## Decision

- **Proof-of-work instead of puzzles**: the browser solves a small key-derivation
  challenge in the background (ALTCHA, PBKDF2/SHA-256, cost 10,000, ~0.5 s of CPU per
  attempt); the visitor sees nothing and types nothing. PoW does not tell humans from
  bots — it prices attempts; it is the second layer on top of the rate limiter, not a
  replacement.
- **Fully self-hosted**: challenges are issued and verified by the gateway itself
  (`GET /v1/user/captcha`, `GET /v1/operator/captcha`); no external service, no
  registration, no calls leaving the installation. The widget (MIT) is vendored per
  [ADR-0012](0012-frontend-stack.md); the server side is the official `altcha` Python
  library (MIT, zero dependencies).
- **Signed, stateless challenges**: each challenge carries an expiry (10 minutes) and an
  HMAC signature; the signing secret is random per process and lives only in memory.
  Solved challenges are one-time — the used-nonce registry lives in process memory with
  the same honest single-process semantics as the rate limiter ([ADR-0003](0003-single-process-supervised.md)):
  nothing to configure, nothing in the database, a restart merely makes browsers solve a
  fresh challenge.
- **Coverage**: sign-in, registration, password-reset request, operator sign-in — the
  mutating anonymous routes. The reset confirmation is already guarded by the one-time
  token from the email. Checks run after the rate limiter (the cheapest defense first)
  and before any business logic; failure answers 400 `captcha_failed`.

## Considered Alternatives

- **External captcha services** (reCAPTCHA, hCaptcha, Turnstile, Friendly Captcha,
  GeeTest, MTCaptcha, CaptchaFox, Yandex SmartCaptcha): all require per-site
  registration and keys, and hand visitor traffic to a third party; rejected for a
  self-installable gateway.
- **Cap** (Apache 2.0, PoW): the verification server is JavaScript (Bun runtime) — a
  foreign service next to a Python backend; rejected.
- **mCaptcha** (Rust, AGPL): pre-1.0 with a slow release cadence; rejected.
- **Classic image captchas** (Python generators): solved reliably by modern vision
  models while annoying humans; rejected.

## Consequences

- Sign-in requires JavaScript with the Web Crypto API (browsers of 2018 and newer);
  accepted for a payment-gateway cabinet.
- One verification costs the server ~2 ms (an HMAC fast path); the browser spends a
  fraction of a second per attempt, invisible behind the button's loading state.
- The widget is the fourth vendored library; its update is a deliberate separate task
  (ADR-0012).
- Tests solve real challenges with a lowered cost (an explicit `create_app` parameter);
  the cryptographic path stays identical.

## Related

- [ADR-0003: Single Backend Process with a Supervising Orchestrator](0003-single-process-supervised.md)
- [ADR-0012: Frontend Stack](0012-frontend-stack.md)
- [HTTP API](../components/http-api.md)
- [User Cabinet Scenarios](../../50-frontend/user-cabinet.md)
- [Operator Panel Scenarios](../../50-frontend/operator-panel.md)
