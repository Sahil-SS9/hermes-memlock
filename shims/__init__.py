"""Harness shims for MemLock — adapters that host the shared core.

Each subpackage adapts ``memlock_core.MemlockService`` to one agent harness's
extension contract. Shims own ALL harness-specific wiring (hook names, payload
shapes, process plumbing); the core stays import-clean and harness-blind.
"""
