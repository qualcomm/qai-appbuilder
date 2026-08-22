# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Language-neutral port registry — the single source of truth for ports.

All network ports used across the three runtimes live in
``<repo_root>/factory/config/ports.json``:

* backend (this Python package) — reads via :func:`load_ports`
* frontend (Vite / TypeScript)  — imports the JSON directly
* desktop (Tauri / Rust)        — ``include_str!`` at compile time

This module is **pure stdlib** (``json`` + ``pathlib``) on purpose so the
thin ``apps.cli.serve`` supervisor can import it without pulling in the
full pydantic ``Settings`` stack. The values it returns feed
``ServerSettings`` defaults (``settings.py``) and the supervisor's
``FALLBACK_PORTS`` (``serve.py``), so ``factory/config/ports.json`` is the
only place a port literal is authored.

Robust by design: a missing / malformed ``ports.json`` never crashes
import — :func:`load_ports` falls back to the built-in :data:`DEFAULTS`
(which mirror the shipped JSON) so a corrupt file degrades gracefully
rather than taking down startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: File name searched for under ``<repo_root>/factory/config/``.
PORTS_FILENAME = "ports.json"

#: Built-in fallback values — MUST mirror ``factory/config/ports.json``. Used only
#: when the JSON file is absent or unreadable so a corrupt file cannot crash
#: startup. Keep in sync with the shipped JSON.
DEFAULTS: dict[str, Any] = {
    "backend": 8989,
    "frontend_dev": 5173,
    "preview": 4173,
    "dev_backend_proxy": 8900,
    "legacy_packaged": 8989,
    "fallbacks": [8989, 28688],
    "cors_extra_origins": [8989, 28688, 5173],
}


def _repo_root_from_here() -> Path:
    """Return the repo root inferred from this file's location.

    ``settings.py`` lives at ``src/qai/platform/config/ports.py`` so the
    repo root is four parents up (config → platform → qai → src → root).
    """

    return Path(__file__).resolve().parents[4]


def ports_path(repo_root: Path | None = None) -> Path:
    """Return the absolute path to ``factory/config/ports.json``.

    Uses ``repo_root`` when given, else infers it from this module's
    location. Pure path computation — does not touch the filesystem.
    """

    root = repo_root if repo_root is not None else _repo_root_from_here()
    return Path(root) / "factory" / "config" / PORTS_FILENAME


def load_ports(repo_root: Path | None = None) -> dict[str, Any]:
    """Load ``factory/config/ports.json`` merged over :data:`DEFAULTS`.

    Any key missing from the JSON (or an absent / unreadable / malformed
    file) falls back to the corresponding :data:`DEFAULTS` value, so callers
    always get a complete mapping and import never crashes on a bad file.
    ``_``-prefixed keys (comments / notes) in the JSON are ignored.
    """

    merged: dict[str, Any] = dict(DEFAULTS)
    target = ports_path(repo_root)
    try:
        with target.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return merged
    if not isinstance(data, dict):
        return merged
    for key, value in data.items():
        if key.startswith("_"):
            continue
        merged[key] = value
    return merged


def backend_port(repo_root: Path | None = None) -> int:
    """The server bind port (SSO-pinned 8989 by default)."""

    return int(load_ports(repo_root)["backend"])


def fallback_ports(repo_root: Path | None = None) -> tuple[int, ...]:
    """Supervisor fallback probe list (``serve.py`` ``FALLBACK_PORTS``)."""

    return tuple(int(p) for p in load_ports(repo_root)["fallbacks"])


def cors_allow_origins(repo_root: Path | None = None) -> tuple[str, ...]:
    """Default CORS allow-list: backend + ``cors_extra_origins``.

    Emits both ``127.0.0.1`` and ``localhost`` origins for each port, in the
    order (backend first, then extras) that the previous static default used.
    """

    ports = load_ports(repo_root)
    ordered: list[int] = [int(ports["backend"])]
    for extra in ports.get("cors_extra_origins", []):
        p = int(extra)
        if p not in ordered:
            ordered.append(p)
    origins: list[str] = []
    for p in ordered:
        origins.append(f"http://127.0.0.1:{p}")
        origins.append(f"http://localhost:{p}")
    return tuple(origins)


__all__ = [
    "DEFAULTS",
    "PORTS_FILENAME",
    "backend_port",
    "cors_allow_origins",
    "fallback_ports",
    "load_ports",
    "ports_path",
]
