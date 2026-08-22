# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem implementation of :class:`ServiceConfigRepositoryPort`.

Persists the GenieAPIService ``service_config.json`` document and resolves
its active on-disk location. The **single source of truth** for the runtime
service config is the copy that ships next to ``GenieAPIService.exe`` inside
the configured install root — the daemon reads it and
``ProcessBackedInferenceService._sync_service_config_model`` writes it.

Policy:

- When a GenieAPIService install root is configured, its
  ``service_config.json`` is located by a breadth-first directory search
  (max 3 levels deep, V1 ``forge_config_manager`` parity) and is the
  authoritative file read/written.
- When GenieAPIService is **not installed** (no install root, or no
  ``service_config.json`` found inside it), there is **no on-disk fallback**:
  :meth:`load` returns the in-memory defaults (read-only, for display only)
  and :meth:`save` fails with a :class:`PreconditionFailedError`. We never
  lazily create a zombie ``data/config/service_config.json`` that the daemon
  would not read.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from qai.model_runtime.domain.service_config import (
    default_service_config,
    deep_merge_defaults,
)
from qai.platform.config import DataPaths
from qai.platform.errors import PreconditionFailedError

logger = logging.getLogger("qai.model_runtime.infrastructure.service_config_repository")

# Filename of the runtime service-config document next to GenieAPIService.exe.
_SERVICE_CONFIG_FILENAME = "service_config.json"

# Error code surfaced when a save is attempted while GenieAPIService is not
# installed (no exe-dir config to write). Mapped to HTTP 412 by the unified
# error handler; the frontend disables the config entrypoints as the primary
# guard, this is the hard backend guarantee.
_NOT_INSTALLED_CODE = "model_runtime.service_not_installed"


class FileServiceConfigRepository:
    """Filesystem-backed :class:`ServiceConfigRepositoryPort` implementation."""

    def __init__(self, *, data_paths: DataPaths) -> None:
        # ``data_paths`` is retained on the (unchanged) constructor contract
        # so the DI wiring stays stable. The runtime config now has a single
        # source of truth next to ``GenieAPIService.exe`` — there is no
        # ``data/config/service_config.json`` fallback for this repository to
        # own, so the DataPaths port is no longer consulted here.
        self._data_paths = data_paths

    # ------------------------------------------------------------------
    # ServiceConfigRepositoryPort
    # ------------------------------------------------------------------

    def resolve_active_path(self, genie_root: str) -> str:
        """V1 parity (forge_config_manager.py:348-388): derive the active
        service_config.json path from *genie_root* (``genie_service.root_path``).

        - If *genie_root* is non-empty and points to an existing directory,
          search it recursively for ``service_config.json`` (max 3 levels)
          so the UI reads the *real* GenieAPIService config; the resolved
          path is the single source of truth (the daemon reads it).
        - Otherwise (GenieAPIService not installed) return ``""`` — there is
          **no on-disk fallback**. Callers use the empty string to mean
          "not installed": :meth:`load` returns in-memory defaults and
          :meth:`save` fails.
        """
        resolved = self._resolve_active_path(genie_root)
        return str(resolved) if resolved is not None else ""

    def load(self, *, path: str | None = None) -> dict[str, Any]:
        """Load the active service-config document, merged onto the defaults.

        - When *path* points to an existing exe-dir ``service_config.json``
          (GenieAPIService installed), read it and deep-merge onto defaults.
        - When *path* is ``None``/empty (GenieAPIService **not installed**),
          return a fresh copy of the in-memory defaults **without writing
          anything to disk** — these are for read-only display. We never
          lazily create a ``data/config/service_config.json`` the daemon
          would not read.
        """
        if not path:
            # Not installed: in-memory defaults only, no disk side effect.
            return default_service_config()
        effective = Path(path)
        if not effective.exists():
            # An exe-dir path was resolved but the file vanished; do not
            # auto-create (installer owns it) — fall back to defaults.
            logger.warning(
                "Resolved service_config.json at %s does not exist; "
                "returning in-memory defaults",
                effective,
            )
            return default_service_config()
        try:
            # ``utf-8-sig`` transparently strips a UTF-8 BOM when present
            # (installer-written files may carry one); plain ``utf-8`` would
            # otherwise leave the BOM in the stream and make ``json.loads``
            # raise — silently discarding the user's real config.
            raw = json.loads(effective.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            # Malformed/unreadable file falls back to defaults, but no longer
            # silently: a warning is logged so the lost config is diagnosable.
            logger.warning(
                "Failed to read service_config.json at %s (%s); "
                "falling back to defaults",
                effective,
                exc,
            )
            return default_service_config()
        return deep_merge_defaults(default_service_config(), raw)

    def save(self, data: dict[str, Any], *, path: str | None = None) -> None:
        """Persist *data* verbatim to the active (exe-dir) path.

        This is a plain write (no implicit merge): the caller is responsible
        for any read-modify-write merging it needs. Mirrors V1's
        ``_save_service_config`` (which simply serialised the prepared
        document to disk).

        When *path* is ``None``/empty (GenieAPIService **not installed**) the
        save is rejected with a :class:`PreconditionFailedError`: there is no
        authoritative exe-dir file to write and we refuse to create a zombie
        ``data/config/service_config.json`` the daemon would never read.
        """
        if not path:
            raise PreconditionFailedError(
                _NOT_INSTALLED_CODE,
                "GenieAPIService is not installed; install it before "
                "configuring the service.",
            )
        effective = Path(path)
        effective.parent.mkdir(parents=True, exist_ok=True)
        # ``indent=4`` mirrors V1 ``_save_service_config`` and matches the
        # installer-owned document's formatting, keeping it readable instead
        # of reflowing the (comment-laden) file to a 2-space layout.
        effective.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal helpers (V1 parity)
    # ------------------------------------------------------------------

    def _resolve_active_path(self, genie_root: str) -> Path | None:
        """Return the exe-dir ``service_config.json`` path, or ``None`` when
        GenieAPIService is not installed (no root / not found)."""
        root_str = (genie_root or "").strip()
        if root_str:
            root = Path(root_str)
            if root.is_dir():
                found = self._find_service_config_in_root(root)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _find_service_config_in_root(
        root: Path, max_depth: int = 3
    ) -> Path | None:
        """V1 parity (forge_config_manager.py:55-82): recursively search for
        ``service_config.json`` under *root* up to *max_depth* directory levels.

        Returns the first match found (breadth-first by sorted name), or None.
        """
        # V1 parity guard: a non-directory root (e.g. a file path) would make
        # the recursive ``iterdir`` raise; bail out cleanly instead.
        if not root.is_dir():
            return None

        def _search(directory: Path, depth: int) -> Path | None:
            if depth < 0:
                return None
            candidate = directory / _SERVICE_CONFIG_FILENAME
            if candidate.is_file():
                return candidate
            try:
                for child in sorted(directory.iterdir()):
                    if child.is_dir() and not child.is_symlink():
                        result = _search(child, depth - 1)
                        if result is not None:
                            return result
            except PermissionError:
                pass
            return None

        return _search(root, max_depth)


__all__ = ["FileServiceConfigRepository"]
