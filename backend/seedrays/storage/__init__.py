"""Domain-level storage interface (see ADR-0006).

Components above this interface never see SQL. Backends: SQLite first,
PostgreSQL and MySQL later. Per-user databases plus the shared registry.
"""
