# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime endpoint file — single source of truth for "where is the API".

Background
----------
``ServerSettings.port`` defaults to ``4099`` (see
``qai.platform.config.settings.ServerSettings.port`` — the value is
pinned by the Okta redirect_uri ``http://localhost:4099/callback``
registered on the authorization server, so any deviation breaks SSO).
``Start.bat`` also passes ``--port 4099`` explicitly to the supervisor
so the actual bind matches the SSO contract. However, on Windows the OS
dynamically reserves random TCP port ranges at boot (Hyper-V / WSL2 /
Docker / WinNAT — visible via
``netsh int ipv4 show excludedportrange protocol=tcp``). Any port inside
such a range fails ``bind()`` with ``WinError 10013`` even though no
process is listening on it. ``apps.cli.serve`` therefore probes a list of
fallback ports at supervisor startup (see ``apps/cli/serve.py``
``FALLBACK_PORTS``) and — when ``--port`` is not pinned explicitly — may
pick a port other than the documented default. Different machines, and
even different reboots of the same machine, can land on different
ports.

That makes the *static* default in ``settings.py`` an unreliable
contract for downstream consumers (``Start.bat`` browser auto-open,
``Uninstall.bat`` reboot ping, the desktop shell). They need the
*actual* host:port the running API is listening on, which only the API
process itself knows.

Design
------
The lifespan writes a small JSON file to
``<data_root>/runtime/server.endpoint.json`` after uvicorn's
``Application startup complete`` and removes it on shutdown. Consumers
read the file and dial whatever it says, falling back gracefully (e.g.
to the documented default ``4099``) when the file is absent (server not
running) or stale.

This is the AGENTS.md §3.10 "State-Truth-First" rule 4 in action: one
authoritative truth source for "where is the API now", not a static
hard-coded port duplicated across launcher / docs / scripts.

Placement
---------
This module lives in the ``qai.platform.process`` shared kernel (pure
stdlib — no FastAPI / context dependency) so BOTH ``apps.*`` (the API
process writing the file) AND init tooling (``scripts/init/uninstall.py``
reading it) can share the same truth source without crossing the
init→apps isolation boundary. ``apps.api._runtime_endpoint`` re-exports
this module for backwards compatibility (INIT-ISO-1, 2026-06-27).

Fields
------
``host`` / ``port`` / ``url``: redundant for convenience; consumers
typically just want the URL string.
``pid``: the API process pid; lets stale-detection check whether the
recorded process is still alive (a future enhancement — current
consumers simply fall back when the URL does not respond).
``started_at``: ISO-8601 UTC timestamp of when the server started
serving traffic. Useful for ``support`` reports.

The file lives under ``data/`` which is ``.gitignore``-d (per the
refactor plan §9.4 — ``data/`` is runtime user state, never source) so
no further ignore entries are needed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any


# Subdirectory under ``<data_root>`` for runtime state files (lock files,
# endpoint files, supervisor breadcrumbs). Created on demand by
# :func:`write_endpoint`. Single-source-of-truth name so consumers and
# tests reference the same constant.
RUNTIME_SUBDIR = "runtime"
ENDPOINT_FILENAME = "server.endpoint.json"


def endpoint_path(data_root: Path) -> Path:
    """Return the absolute path to ``server.endpoint.json`` under ``data_root``.

    Pure path computation — does not touch the filesystem.
    """

    return Path(data_root) / RUNTIME_SUBDIR / ENDPOINT_FILENAME


def write_endpoint(
    data_root: Path,
    *,
    host: str,
    port: int,
    pid: int | None = None,
    now: _dt.datetime | None = None,
) -> Path:
    """Atomically write the endpoint file and return its path.

    Atomicity matters because consumers (notably ``Start.bat`` polling
    for the file to appear) may read the file the instant it shows up;
    a half-written file would parse as broken JSON. We write to a
    sibling ``.tmp`` file and ``os.replace`` it into place — that is
    atomic on both POSIX and Windows.

    The parent ``runtime/`` directory is created on demand.
    """

    timestamp = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(
        timespec="microseconds"
    )
    payload: dict[str, Any] = {
        "host": host,
        "port": int(port),
        "url": f"http://{host}:{int(port)}",
        "pid": int(pid) if pid is not None else None,
        "started_at": timestamp,
        "schema_version": 1,
    }
    target = endpoint_path(data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use ``NamedTemporaryFile`` in the same directory so ``os.replace``
    # stays on the same filesystem (cross-FS replace is not atomic on
    # POSIX; on Windows we simply avoid that footgun).
    fd, tmp_path = tempfile.mkstemp(
        prefix=ENDPOINT_FILENAME + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        # Best-effort cleanup on any failure (Permission / Disk full /
        # KeyboardInterrupt). The temp file would otherwise leak under
        # ``runtime/``.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def clear_endpoint(data_root: Path) -> bool:
    """Delete the endpoint file if present. Returns ``True`` iff removed.

    Best-effort: missing file is success (idempotent), permission /
    locked-file errors are swallowed and returned as ``False`` so the
    caller (lifespan ``finally``) can keep tearing down without raising.
    """

    target = endpoint_path(data_root)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def read_endpoint(data_root: Path) -> dict[str, Any] | None:
    """Read and parse the endpoint file, returning ``None`` if absent / unreadable.

    Used by Python-side consumers (``scripts/init/uninstall.py``).
    Always returns ``None`` rather than raising — callers that want to
    dial the URL should fall back to a sensible default (e.g. the
    documented default ``4099``) when this returns ``None`` so a stale-or-absent
    endpoint never prevents a graceful shutdown / uninstall.
    """

    target = endpoint_path(data_root)
    try:
        with target.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


__all__ = [
    "ENDPOINT_FILENAME",
    "RUNTIME_SUBDIR",
    "clear_endpoint",
    "endpoint_path",
    "read_endpoint",
    "write_endpoint",
]
