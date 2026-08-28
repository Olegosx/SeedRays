[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0013-backend-stack.md)

# ADR-0013: Backend Stack

**Status:** accepted

## Context

Backend implementation begins. The gateway is financial-sector software: input validation is
a safety matter, the dependency list must stay short and deliberate, and the cryptographic
libraries are the most consequential choice of the project. Facts verified against sources
(GitHub/PyPI, August 2026): bip-utils 2.12.2 released 2026-08-13 (MIT, BIP39/32/44 and
addresses for 100+ coins including TRON and Ethereum); coincurve 21.0.0 (binding to
libsecp256k1, the audited Bitcoin Core C library; used by Ethereum and libp2p); mnemonic
0.21 (Trezor's reference BIP39 implementation). Target machine: Ubuntu 26.04 LTS, system
Python 3.14.4 — however, installation on 3.14 failed in practice: coincurve (the secp256k1
engine bip-utils depends on) ships no prebuilt wheels for CPython 3.14, and its source
build is broken against current cffi. The project therefore targets Python 3.13.

## Decision

- **Python 3.13** (installed from the deadsnakes PPA) — the newest version fully supported
  by the dependency chain: coincurve provides prebuilt wheels for 3.9–3.13 only, and 3.13
  is the latest version bip-utils declares. The project runs in a virtual environment.
  Moving to 3.14 once coincurve ships cp314 wheels is a one-line change.
- **FastAPI + uvicorn** — the web framework: declarative pydantic validation of every input
  field before it reaches the logic (for a financial API this is protection, not
  convenience); automatic OpenAPI specification — free, always-current API documentation
  for application developers; async fits the single-process model of
  [ADR-0003](0003-single-process-supervised.md) with the watcher as a background task.
- **SQLAlchemy Core + Alembic** — the database layer: one query code for
  SQLite/PostgreSQL/MySQL (exactly the plan of
  [ADR-0006](0006-storage-abstraction.md), whose open library question this closes);
  Alembic handles migrations, including the loop over N user databases. Core, not ORM:
  explicit queries, no object-mapping magic. Async SQLite access uses the **aiosqlite**
  driver (a thin MIT wrapper over the standard library's sqlite3); asyncpg will play the
  same role when PostgreSQL support arrives.
- **httpx** — async HTTP client to chain data providers. Provider REST APIs are called
  directly behind our own chain abstraction; wrapper libraries (e.g. tronpy) are not used.
- **bip-utils** — BIP39/BIP32/BIP44 and address encoding for TRON/EVM. The version is
  pinned; the official BIP39/BIP32/BIP44 test vectors are part of our test suite as a
  safety net — any breakage or substitution fails the tests immediately. As of 2.12.2 the
  author declares Python up to 3.13 in classifiers, which matches the chosen interpreter.
- **argon2-cffi** — password hashing for cabinet/operator sign-in (Argon2, the Password
  Hashing Competition winner and current recommendation for new systems).
- **argparse** (standard library) — console commands; zero dependencies at our CLI scale.
- Dependency versions are pinned in `pyproject.toml` at environment creation; updating a
  dependency is a deliberate separate task.

## Alternatives Considered

- **Flask** — minimal, but synchronous and with hand-written validation of financial
  inputs; rejected. **aiohttp** — async and self-contained, but no validation layer and no
  OpenAPI generation; rejected.
- **Hand-rolled DB layer over drivers** — zero dependencies, but three SQL dialects and
  migration machinery maintained by hand; rejected.
- **Minimal crypto bricks** (mnemonic + coincurve + own BIP32/44 implementation) — each
  brick maximally reputable, but derivation code written by ourselves: for a financial
  product "don't roll your own crypto" outweighs the extra dependency; rejected.
- **tronpy and similar chain wrappers** — a foreign wrapper inside our own abstraction adds
  a dependency without value; rejected.
- **bcrypt** — works, but a previous-generation standard next to Argon2; rejected.
- **click/typer** — nothing needed beyond argparse at this scale; rejected.

## Consequences

- Eight runtime dependencies for the whole backend: fastapi, uvicorn, sqlalchemy, alembic,
  httpx, bip-utils, argon2-cffi, aiosqlite (pytest in the dev group).
- Versions pinned at environment creation (2026-08-29): fastapi 0.141.1, uvicorn 0.52.4,
  SQLAlchemy 2.0.52, alembic 1.19.1, httpx 0.28.1, bip-utils 2.12.2, argon2-cffi 25.1.0,
  aiosqlite 0.22.1; pytest 9.1.1 (dev).
- The single-maintainer risk of bip-utils is mitigated by version pinning and standard test
  vectors.
- The backend code is async throughout (FastAPI + httpx + watcher tasks).

## Related

- [Architecture Overview](../overview.md)
- [ADR-0003: Single Backend Process with a Supervising Orchestrator](0003-single-process-supervised.md)
- [ADR-0006: Storage Abstraction at the Domain-Operation Level](0006-storage-abstraction.md)
- [ADR-0012: Frontend Stack](0012-frontend-stack.md)
