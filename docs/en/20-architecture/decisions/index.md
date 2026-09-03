[Index](../../index.md) · [Architecture Overview](../overview.md) · [Русская версия](../../../ru/20-architecture/decisions/index.md)

# Architecture Decision Records

Log of architecture decisions. Each record captures one decision: its context, the options
considered, the choice made and its consequences.

## Log

| ID | Title | Status |
|----|-------|--------|
| [ADR-0001](0001-library-first-core.md) | Library-First Core with Thin Adapters | accepted |
| [ADR-0002](0002-watch-only-online-part.md) | Watch-Only Online Part; Key Generation Paths | accepted |
| [ADR-0003](0003-single-process-supervised.md) | Single Backend Process with a Supervising Orchestrator | accepted |
| [ADR-0004](0004-two-api-groups.md) | Two API Route Groups with Different Access Rights | accepted, extended by ADR-0005 |
| [ADR-0005](0005-multi-user-model.md) | Multi-User Model — Per-User Database and Directory, Three Roles | accepted, extended by ADR-0008 |
| [ADR-0006](0006-storage-abstraction.md) | Storage Abstraction at the Domain-Operation Level | accepted, extended by ADR-0013 |
| [ADR-0007](0007-address-centric-watcher.md) | Address-Centric Watcher Pass | accepted, refined by ADR-0018 |
| [ADR-0008](0008-shared-registry-db.md) | Shared Registry Database | accepted, extended by ADR-0010 |
| [ADR-0009](0009-address-bindings-primary-mode.md) | Persistent Address Bindings as the Primary Mode | accepted, refined by ADR-0010 |
| [ADR-0010](0010-networks-assets-financial-data.md) | Networks, Assets and Financial Data Structures | accepted, partly superseded by ADR-0017 |
| [ADR-0011](0011-application-api-principles.md) | Application API Principles | accepted |
| [ADR-0012](0012-frontend-stack.md) | Frontend Stack — No-Build Static Files with Vendored Libraries | accepted |
| [ADR-0013](0013-backend-stack.md) | Backend Stack | accepted |
| [ADR-0014](0014-key-standards.md) | Key Generation and Derivation Standards | accepted |
| [ADR-0015](0015-tron-provider.md) | TRON Data Provider — TronGrid; Chain Source Interface | accepted, extended by ADR-0018 |
| [ADR-0016](0016-config-layers.md) | Configuration Layers | accepted |
| [ADR-0017](0017-universal-tx-model.md) | Universal Transaction Model — Two Tables by Physical Location | accepted |
| [ADR-0018](0018-range-scanning.md) | Range Scanning as the Watcher's Primary Acquisition | accepted |
| [ADR-0019](0019-frontend-localization.md) | Frontend Localization — Client-Side Dictionaries | accepted |
| [ADR-0020](0020-mail-provider.md) | Outgoing Mail — a Third-Party Service Behind an Abstraction | accepted |

## Related

- [Architecture Overview](../overview.md)
