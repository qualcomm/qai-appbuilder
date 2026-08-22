"""One-shot runtime data migrators.

Distinct from ``tools/build/factory_compiler`` (which migrates the *v1
JSON config tree* → the *v2 layered TOML tree* at build time). The
migrators here fix up **runtime user data** in an installed v2 tree —
typically moving files that were previously written to the wrong
location by an earlier v2 build so a fresh boot on the new build finds
its state where it now expects it.

Every module in this package MUST be idempotent: running it more than
once (either explicitly or through the lifespan startup hook) is a
no-op after the first successful pass. See individual modules for the
sentinel each one uses to detect "already migrated".
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
