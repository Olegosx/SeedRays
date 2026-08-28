"""Watcher: monitors balances and incoming payments across all users' wallets.

Built as a single pass over an address work list plus a continuous loop
calling that pass (see ADR-0007).
"""
