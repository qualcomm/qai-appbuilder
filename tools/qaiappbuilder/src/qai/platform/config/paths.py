# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DataPaths — the single port for resolving runtime filesystem paths.

This is THE place that knows the on-disk layout for ``data/``. Business
code (``src/qai/<context>/`` and ``apps/``) MUST NOT join paths against
``data/`` directly; instead it injects a ``DataPaths`` instance and asks
for the resolved location.

Layout (refactor-plan v2.5 §9.4 / inventory 05):

    <data_dir>/
    ├── db/
    │   ├── qai.db
    │   └── backups/
    ├── blobs/
    │   ├── chat/<yyyy-mm-dd>/<conv_id>/
    │   ├── app_builder/<yyyy-mm-dd>/
    │   └── uploads/{audio,images}/<yyyy-mm-dd>/
    ├── audit/
    ├── cache/
    ├── prefs/
    ├── tmp/
    └── secrets/
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Final

# The per-user application data namespace under ``%LOCALAPPDATA%``. Kept as
# a single module constant so every consumer (installer, git bootstrap,
# secret store, FileGuard whitelist) resolves the SAME directory name — the
# runtime data root the ARM64 venv + ``Setup.bat`` create.
LOCALAPPDATA_QAI_DIRNAME: Final[str] = "QAIModelBuilder"

# Recognised top-level subdirectories. Anything else is rejected to keep
# the layout disciplined.
_KNOWN_SUBDIRS: Final[frozenset[str]] = frozenset(
    {"db", "blobs", "audit", "cache", "prefs", "tmp", "secrets"}
)


class DataPaths:
    """Resolve filesystem paths under a single ``data_dir`` root.

    The constructor accepts a ``data_dir`` ``Path`` (typically taken from
    ``Settings.data.data_dir``) and exposes typed methods that build
    sub-paths. Methods do NOT touch the filesystem; call ``ensure(...)``
    where directory creation is desired.
    """

    def __init__(self, data_dir: Path) -> None:
        if not isinstance(data_dir, Path):
            raise TypeError("data_dir must be a Path")
        self._root = data_dir

    # ------------------------------------------------------------------
    # Top-level accessors
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def db_dir(self) -> Path:
        return self._root / "db"

    @property
    def blobs_dir(self) -> Path:
        return self._root / "blobs"

    @property
    def audit_dir(self) -> Path:
        return self._root / "audit"

    @property
    def cache_dir(self) -> Path:
        return self._root / "cache"

    @property
    def prefs_dir(self) -> Path:
        return self._root / "prefs"

    @property
    def tmp_dir(self) -> Path:
        return self._root / "tmp"

    @property
    def secrets_dir(self) -> Path:
        return self._root / "secrets"

    @property
    def config_dir(self) -> Path:
        """``data/config/`` — runtime-editable JSON config documents.

        Holds operator-editable config files written by the running app
        (e.g. ``service_config.json``, ``qairt_env.json``). This is NOT a
        date-partitioned blob root and is intentionally separate from the
        installer-owned ``config/`` directory at the repo root. Kept off
        ``_KNOWN_SUBDIRS`` (it is not auto-created by ``ensure_top_levels``;
        callers create it on first write via ``ensure(...)``).
        """
        return self._root / "config"

    # ------------------------------------------------------------------
    # App Builder — user-imported Pack storage (data/ tree)
    # ------------------------------------------------------------------
    #
    # Layering (needs-2 / decision "Plan C — conservative"):
    #
    #   Built-in Packs (release-distributed):
    #     Pack definition (manifest / runner / SKILL): factory/chat_features/app-builder/models/<id>/
    #     Weight .bin files:                           <repo_root>/models/<id>/*.bin
    #
    #   User-imported Packs (per-user runtime state):
    #     Pack definition (manifest / runner / SKILL): <data_dir>/app_builder/user_models/<id>/
    #     Weight .bin files:                           <data_dir>/app_builder/user_model_weights/<id>/*.bin
    #
    # Why user data lives here and NOT in the source tree:
    #   1. Survives software upgrade / reinstall / uninstall (data/ is per-user
    #      state; the source tree is release artefacts).
    #   2. Never enters the release zip (data/ is excluded by
    #      scripts/release/manifest.toml [exclude_paths]).
    #   3. Never gets committed to git (data/ is .gitignore'd repo-root).
    #   4. Enforces the State-Truth-First contract "delete user model → all
    #      files gone" without touching factory/, whose contents are owned by
    #      the release channel.
    #   5. Prevents the built-in-seed startup hook from mis-promoting a
    #      user Pack into a built-in row when the DB is missing/reset
    #      (the seed scanner only looks at factory/chat_features/app-builder/models/,
    #      never at these two roots — see apps/api/lifespan.py
    #      ``_resolve_seed_pack_root``).

    @property
    def app_builder_user_pack_root(self) -> Path:
        """User-imported App Builder Packs (definition side).

        Import commit lands each user Pack at
        ``<data_dir>/app_builder/user_models/<id>/`` — a data-only location
        that mirrors the on-disk layout of the built-in Pack root
        (``factory/chat_features/app-builder/models/<id>/``) so every adapter reading
        ``manifest.json`` / ``runner.py`` / ``SKILL.md`` from a Pack
        directory works identically whether the Pack is built-in or user-
        imported.

        Directory is NOT created here; callers use ``ensure(...)`` on first
        write (mirrors the ``config_dir`` policy — off ``_KNOWN_SUBDIRS``).
        """
        return self._root / "app_builder" / "user_models"

    @property
    def app_builder_user_weights_root(self) -> Path:
        """Anchor for user-Pack weight ``.bin`` files.

        User-Pack manifest ``installPath`` fields resolve relative to this
        root (mirrors how built-in Packs resolve ``installPath`` against
        ``<repo_root>/models/``, but rooted at the user data tree so a
        ``delete user model`` call can remove every trace of the Pack
        without touching the source tree).

        Directory is NOT created here; callers use ``ensure(...)`` on first
        write.
        """
        return self._root / "app_builder" / "user_model_weights"

    # ------------------------------------------------------------------
    # Concrete file paths
    # ------------------------------------------------------------------

    def db_path(self, name: str = "qai.db") -> Path:
        """Path to a SQLite database under ``data/db/``."""
        _validate_safe_filename(name)
        return self.db_dir / name

    def db_backup_dir(self) -> Path:
        return self.db_dir / "backups"

    def cache_file(self, name: str) -> Path:
        """Path to a remote-fetched cache file (e.g. model_catalog.json)."""
        _validate_safe_filename(name)
        return self.cache_dir / name

    def secret_file(self, name: str) -> Path:
        """Path to a fallback secret file (used when keyring is unavailable)."""
        _validate_safe_filename(name)
        return self.secrets_dir / name

    def prefs_file(self, name: str) -> Path:
        _validate_safe_filename(name)
        return self.prefs_dir / name

    def config_file(self, name: str) -> Path:
        """Path to a runtime config document under ``data/config/``."""
        _validate_safe_filename(name)
        return self.config_dir / name

    # ------------------------------------------------------------------
    # Blob layout (per context, partitioned by date)
    # ------------------------------------------------------------------

    def blob_dir(
        self,
        context: str,
        *,
        on_date: date | None = None,
        subkey: str | None = None,
    ) -> Path:
        """Resolve a blob directory for a bounded context.

        Parameters:
            context: a fixed identifier (``"chat"``, ``"app_builder"``,
                ``"uploads"``, ``"outputs"``, ``"images"``).
            on_date: the partition date; defaults to today (UTC). Use
                ``None`` for non-date-partitioned roots.
            subkey: an optional sub-identifier (e.g. conversation id).
        """
        _validate_safe_filename(context)
        path = self.blobs_dir / context
        if on_date is not None:
            path = path / on_date.isoformat()
        if subkey is not None:
            _validate_safe_filename(subkey)
            path = path / subkey
        return path

    def upload_dir(
        self,
        kind: str,
        *,
        on_date: date | None = None,
    ) -> Path:
        """Convenience for ``blob_dir("uploads")/{kind}/<date>``."""
        _validate_safe_filename(kind)
        if kind not in {"audio", "images"}:
            raise ValueError(f"unsupported upload kind: {kind!r}")
        path = self.blobs_dir / "uploads" / kind
        if on_date is not None:
            path = path / on_date.isoformat()
        return path

    def audit_log_path(self, context: str, *, on_date: date) -> Path:
        """``data/audit/<context>/<yyyy-mm-dd>.jsonl``."""
        _validate_safe_filename(context)
        return self.audit_dir / context / f"{on_date.isoformat()}.jsonl"

    # ------------------------------------------------------------------
    # Filesystem actions (kept minimal)
    # ------------------------------------------------------------------

    def ensure(self, path: Path) -> Path:
        """Create ``path`` (and parents) if missing. Returns ``path``."""
        # Defensive: the path must be inside this root.
        try:
            path.resolve().relative_to(self._root.resolve())
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"Refusing to create {path!r}: outside data_dir {self._root!r}"
            ) from exc
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_top_levels(self) -> None:
        """Create all known top-level subdirectories. Idempotent."""
        for name in _KNOWN_SUBDIRS:
            self.ensure(self._root / name)


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def resolve_localappdata_qai_root() -> Path | None:
    """Resolve ``%LOCALAPPDATA%\\QAIModelBuilder`` (the per-user data root).

    This is the single shared resolver for the per-user application data
    namespace (previously resolved ad-hoc via
    ``os.environ.get("LOCALAPPDATA")`` in several call sites). Returns the
    resolved :class:`Path`, or ``None`` when ``LOCALAPPDATA`` is unset /
    empty (non-Windows dev boxes, stripped environments) so callers can
    gracefully skip rather than crash — mirrors the empty-env fallback the
    git-bootstrap / uninstall scripts already use.

    Does NOT touch the filesystem; the returned directory may not exist.
    """
    raw = os.environ.get("LOCALAPPDATA", "")
    if not raw or not raw.strip():
        return None
    return Path(raw) / LOCALAPPDATA_QAI_DIRNAME


def _validate_safe_filename(name: str) -> None:
    """Reject path traversal / separators / NULs in user-provided names.

    Mirrors (a subset of) ``qai.platform.io_validator.paths.assert_safe_filename``
    but kept inline to avoid a hard dependency on io_validator at startup.
    """
    if not name:
        raise ValueError("name must not be empty")
    if "/" in name or "\\" in name:
        raise ValueError(f"name must not contain path separators: {name!r}")
    if "\x00" in name:
        raise ValueError("name must not contain NUL")
    if name in {".", ".."}:
        raise ValueError(f"name must not be a relative directory marker: {name!r}")
