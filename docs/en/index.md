# SeedRays Documentation

Entry point for the project documentation. Every document in this tree is listed here.

[Русская версия](../ru/index.md)

## Contents

- [Overview](00-overview.md)
- Requirements
  - [Functional Requirements](10-requirements/functional.md)
  - [Non-Functional Requirements](10-requirements/non-functional.md)
- Architecture
  - [Architecture Overview](20-architecture/overview.md)
  - [Data Model](20-architecture/data-model.md)
  - Components
    - [Orchestrator](20-architecture/components/orchestrator.md)
    - [Key Generator](20-architecture/components/key-generator.md)
    - [Derivation Module](20-architecture/components/derivation.md)
    - [Watcher](20-architecture/components/watcher.md)
    - [Chain Abstraction](20-architecture/components/chain-abstraction.md)
    - [Storage Layer](20-architecture/components/storage.md)
    - [HTTP API](20-architecture/components/http-api.md)
  - [Architecture Decision Records](20-architecture/decisions/index.md)
    - [ADR-0001: Library-First Core with Thin Adapters](20-architecture/decisions/0001-library-first-core.md)
    - [ADR-0002: Watch-Only Online Part; Key Generation Paths](20-architecture/decisions/0002-watch-only-online-part.md)
    - [ADR-0003: Single Backend Process with a Supervising Orchestrator](20-architecture/decisions/0003-single-process-supervised.md)
    - [ADR-0004: Two API Route Groups with Different Access Rights](20-architecture/decisions/0004-two-api-groups.md)
    - [ADR-0005: Multi-User Model — Per-User Database and Directory, Three Roles](20-architecture/decisions/0005-multi-user-model.md)
    - [ADR-0006: Storage Abstraction at the Domain-Operation Level](20-architecture/decisions/0006-storage-abstraction.md)
    - [ADR-0007: Address-Centric Watcher Pass](20-architecture/decisions/0007-address-centric-watcher.md)
    - [ADR-0008: Shared Registry Database](20-architecture/decisions/0008-shared-registry-db.md)
    - [ADR-0009: Persistent Address Bindings as the Primary Mode](20-architecture/decisions/0009-address-bindings-primary-mode.md)
    - [ADR-0010: Networks, Assets and Financial Data Structures](20-architecture/decisions/0010-networks-assets-financial-data.md)
    - [ADR-0011: Application API Principles](20-architecture/decisions/0011-application-api-principles.md)
    - [ADR-0012: Frontend Stack — No-Build Static Files with Vendored Libraries](20-architecture/decisions/0012-frontend-stack.md)
    - [ADR-0013: Backend Stack](20-architecture/decisions/0013-backend-stack.md)
- Security
  - [Key Management](30-security/key-management.md)
  - [Threat Model](30-security/threat-model.md)
- Operations
  - [Deployment](40-operations/deployment.md)
  - [Monitoring](40-operations/monitoring.md)
- [Glossary](glossary.md)
