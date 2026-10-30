# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem implementation of :class:`ServiceConfigRepositoryPort`.

Persists the GenieAPIService ``service_config.json`` document and resolves
its active on-disk location. The **single source of truth** for the runtime
service config is the copy that ships next to ``GenieAPIService.exe`` inside
the configured install root — the daemon reads it and this repository
is the only writer.

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
import os
from pathlib import Path
from typing import Any

from qai.model_runtime.domain.service_config import (
    default_service_config,
    deep_merge_defaults,
    strip_legacy_model_slot_keys,
)
from qai.platform.config import DataPaths
from qai.platform.errors import PreconditionFailedError

logger = logging.getLogger("qai.model_runtime.infrastructure.service_config_repository")

# Filename of the runtime service-config document next to GenieAPIService.exe.
_SERVICE_CONFIG_FILENAME = "service_config.json"

# Suffix of the backup taken before the legacy model-slot keys are stripped
# (``service_config.json.bak-legacy-models``). Never overwritten, so the very
# first pre-migration copy survives.
_LEGACY_BACKUP_SUFFIX = ".bak-legacy-models"

# Suffix of the marker recording that the one-time legacy model-slot migration
# has completed for this install (``service_config.json.legacy-models-migrated``).
# While it exists the migration neither reads nor writes service_config.json,
# so a ``models[]`` the user hand-writes later (a real daemon capability) is
# never stripped again.
_LEGACY_MIGRATED_MARKER_SUFFIX = ".legacy-models-migrated"

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

    @staticmethod
    def strip_legacy_model_slots(*, path: str | Path | None) -> list[str]:
        """One-time migration: strip the retired ``models`` / ``default_model`` keys.

        See :data:`~qai.model_runtime.domain.service_config.LEGACY_MODEL_SLOT_KEYS`
        for why stale slots must not reach the daemon. Meant to run right
        before a daemon spawn on the exact file the daemon reads
        (``<install_dir>/service_config.json``) — never from a read-only GET.
        Static (no repository state) so the spawn path can call it without
        a ``DataPaths``-bound instance.

        Runs at most once per install, tracked by the marker
        ``service_config.json.legacy-models-migrated`` next to the file:

        - Marker present: no-op without reading or writing the config;
          returns ``[]``. Multi-model slots are a real daemon capability, so
          a ``models[]`` the user writes after the migration is preserved.
        - Not installed, file missing/unreadable/unparsable/not an object:
          no write and **no marker** (retried on the next spawn); ``[]``.
        - Already clean: no write, mtime untouched, marker written; ``[]``.
        - Otherwise the original bytes are first copied to
          ``service_config.json.bak-legacy-models`` (only if that backup does
          not exist yet), then the document is rewritten without the keys:
          UTF-8 without BOM, the file's own indent width and newline style,
          via a temp file + ``os.replace`` so the daemon never reads a
          half-written file; the marker is written afterwards. Returns the
          removed keys. A failed rewrite writes no marker (retried next spawn).

        Failures are logged and swallowed: a stale config must never block the
        daemon from starting. A marker-write failure is logged only; the next
        spawn re-runs the strip. For the common case (nothing changed on disk
        in between) that rerun finds the keys already gone and is a true
        no-op (lands in the "already clean" branch above). The one exception:
        if the USER deliberately writes ``models[]`` back during the window
        before the marker exists, the rerun does not know that and strips it
        again — a real, if rare, cost of the marker write having failed. The
        same gap applies to the "no marker" branch above (missing /
        unreadable / unparsable file): a ``models[]`` the user writes into a
        freshly (re)created file before the next spawn is likewise stripped
        once, retroactively, the first time this method can actually parse it.
        """
        if not path:
            return []
        effective = Path(path)
        marker = effective.with_name(effective.name + _LEGACY_MIGRATED_MARKER_SUFFIX)
        if marker.exists():
            logger.debug(
                "service_config.legacy_model_slots_skipped path=%s reason=already_migrated "
                "marker=%s",
                effective,
                marker,
            )
            return []
        try:
            raw_bytes = effective.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning(
                "service_config.legacy_model_slots_skipped path=%s reason=read_failed error=%s",
                effective,
                exc,
            )
            return []
        try:
            text = raw_bytes.decode("utf-8-sig")
            doc = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "service_config.legacy_model_slots_skipped path=%s reason=parse_failed error=%s",
                effective,
                exc,
            )
            return []
        if not isinstance(doc, dict):
            logger.warning(
                "service_config.legacy_model_slots_skipped path=%s reason=not_an_object",
                effective,
            )
            return []
        removed = strip_legacy_model_slot_keys(doc)
        if not removed:
            _write_migrated_marker(marker, config_path=effective, removed=[])
            return []

        backup = effective.with_name(effective.name + _LEGACY_BACKUP_SUFFIX)
        tmp = effective.with_name(effective.name + ".tmp-legacy-models")
        try:
            backup_created = False
            if not backup.exists():
                backup.write_bytes(raw_bytes)
                backup_created = True
            newline = "\r\n" if "\r\n" in text else "\n"
            body = json.dumps(doc, indent=_detect_indent(text), ensure_ascii=False)
            if newline != "\n":
                body = body.replace("\n", newline)
            if text.endswith(("\n", "\r\n")):
                body += newline
            tmp.write_bytes(body.encode("utf-8"))
            os.replace(tmp, effective)
        except OSError as exc:
            logger.warning(
                "service_config.legacy_model_slots_skipped path=%s reason=write_failed "
                "removed=%s error=%s",
                effective,
                removed,
                exc,
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return []
        logger.info(
            "service_config.legacy_model_slots_stripped path=%s removed=%s "
            "backup=%s backup_created=%s marker=%s",
            effective,
            removed,
            backup,
            backup_created,
            marker,
        )
        _write_migrated_marker(marker, config_path=effective, removed=removed)
        return removed

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


def _write_migrated_marker(marker: Path, *, config_path: Path, removed: list[str]) -> None:
    """Record that the one-time legacy-slot migration completed (best effort)."""
    try:
        marker.write_text(
            json.dumps({"migration": "legacy_model_slots", "removed": removed}) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "service_config.legacy_model_slots_marker_write_failed path=%s marker=%s "
            "error=%s",
            config_path,
            marker,
            exc,
        )
        return
    logger.info(
        "service_config.legacy_model_slots_migrated path=%s marker=%s removed=%s",
        config_path,
        marker,
        removed,
    )


def _detect_indent(text: str) -> int:
    """Indent width of the first indented line in *text* (default 4, V1 format)."""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) != len(line):
            return len(line) - len(stripped)
    return 4


__all__ = ["FileServiceConfigRepository"]


__all__ = ["FileServiceConfigRepository"]
