# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Atomic ``qairt_env.json`` read/activate/rollback (Locked Contract 12).

:class:`QairtConfigRepository` owns exactly one on-disk ``qairt_env.json``
for its whole lifetime — either the file the composition root already
resolved (canonical ``data/config/qairt_env.json`` or the legacy dev-
checkout ``config/qairt_env.json``), or, when neither exists yet, a fresh
canonical ``data/config/qairt_env.json`` seeded from the factory defaults
seed. It never creates or revives the legacy location itself.

:meth:`activate` rewrites only the SDK root: it sets canonical
``qairt_root`` and drops any stale legacy ``qairt_sdk_root`` so the file
never carries two disagreeing root keys, while leaving every other field
(``python_runtime_venv``, ``qairt_runtime_subdir``, ``_version``, and any
unrecognised key) byte-for-byte as the caller wrote it. :meth:`snapshot`
and :meth:`restore` provide byte-exact rollback for a failed switch: the
raw bytes captured before the switch are written back verbatim, using the
same same-directory atomic writer as :meth:`activate` (unique exclusive
temp file, ``flush`` + ``os.fsync``, ``os.replace``, cleanup on failure —
a reader never observes a half-written file, and a failed write leaves no
temp file behind).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qai.platform.qairt_switch.versioning import QairtVersion


class QairtConfigError(RuntimeError):
    """Base for all :class:`QairtConfigRepository` failures.

    ``error_kind``/``error_hint`` are the allowlisted (path-free)
    classification :func:`qai.platform.qairt_switch.service._classify_switch_failure`
    surfaces to the HTTP job DTO. ``args[0]`` (the exception message) MAY
    contain a local filesystem path for server-side logging and MUST
    NEVER be forwarded to a client; subclasses pass their own generic
    ``hint`` so no path ever reaches ``error_hint``.
    """

    def __init__(self, message: str, *, kind: str, hint: str) -> None:
        super().__init__(message)
        self.error_kind = kind
        self.error_hint = hint


class QairtConfigPathError(QairtConfigError):
    """The requested QAIRT install directory is invalid.

    Raised when the ``install_root / version`` target is not an existing
    directory, or does not resolve to a path beneath the discovered
    ``install_root``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            kind="target_path_invalid",
            hint="The selected QAIRT SDK directory could not be validated.",
        )


class QairtConfigMalformedError(QairtConfigError):
    """The active ``qairt_env.json`` is not valid UTF-8 JSON, or has no root key."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            kind="config_malformed",
            hint="The QAIRT configuration file could not be read.",
        )


@dataclass(frozen=True, slots=True)
class QairtConfigSnapshot:
    """A byte-exact capture of the active ``qairt_env.json``, taken before a switch.

    ``raw_bytes`` is the file's exact original content, restored verbatim by
    :meth:`QairtConfigRepository.restore`. ``active_root`` is the QAIRT SDK
    root directory that was configured at capture time (parsed from
    ``qairt_root``, or the legacy ``qairt_sdk_root``) — informational for
    the caller (for example rollback/audit logging); :meth:`restore` does
    not need it, since a repository always writes back to the same fixed
    config path for its whole lifetime.
    """

    raw_bytes: bytes
    active_root: Path


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a same-directory atomic replace.

    Uses a unique, exclusively-created temp file in ``path``'s own
    directory (so the final ``os.replace`` is a same-filesystem rename,
    never a cross-device copy), flushes and ``fsync``s the data before the
    rename, and removes the temp file on any failure — the destination is
    always either the old content or the new content, and no temp file is
    ever left behind.
    """

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _parse_active_root(raw: bytes, *, source: Path) -> Path:
    """Parse the configured SDK root out of a ``qairt_env.json`` payload.

    Accepts both the canonical ``qairt_root`` key and the legacy
    ``qairt_sdk_root`` key (mirroring
    :func:`qai.platform.qairt_versions.resolve_qairt_installation`).
    Raises :class:`QairtConfigMalformedError` when the payload is not valid
    UTF-8 JSON or carries no recognisable root key.
    """

    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise QairtConfigMalformedError(f"{source} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise QairtConfigMalformedError(f"{source} does not contain a JSON object")
    root = payload.get("qairt_root") or payload.get("qairt_sdk_root")
    if not isinstance(root, str) or not root:
        raise QairtConfigMalformedError(
            f"{source} has neither a 'qairt_root' nor a 'qairt_sdk_root' key"
        )
    return Path(os.path.expandvars(os.path.expanduser(root)))


def _resolve_within_root(target: Path, install_root: Path) -> Path:
    """Resolve ``target`` and require it to sit beneath ``install_root``.

    Raises :class:`QairtConfigPathError` when the resolved target escapes
    the resolved install root (defense-in-depth against a caller-supplied
    ``install_root`` that resolves through an unexpected reparse point).
    """

    resolved_root = install_root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise QairtConfigPathError(
            f"{resolved_target} is not beneath discovered install root {resolved_root}"
        ) from exc
    return resolved_target


class QairtConfigRepository:
    """Owns one ``qairt_env.json`` file: snapshot, atomic activate, atomic restore."""

    def __init__(
        self,
        *,
        existing_config_path: Path | None,
        canonical_config_path: Path,
        factory_defaults_path: Path,
    ) -> None:
        """Bind this repository to exactly one config file for its lifetime.

        ``existing_config_path`` is the path the composition root already
        resolved (canonical or legacy), or ``None`` when neither exists.
        When ``None``, a fresh file is created at ``canonical_config_path``
        (never at any legacy location) seeded verbatim from
        ``factory_defaults_path``'s bytes.
        """

        self._config_path = (
            existing_config_path if existing_config_path is not None else canonical_config_path
        )
        self._factory_defaults_path = factory_defaults_path
        self._ensure_config_file()

    def _ensure_config_file(self) -> None:
        """Create ``self._config_path`` from factory defaults if it is missing."""

        if self._config_path.exists():
            return
        defaults = self._factory_defaults_path.read_bytes()
        _atomic_write_bytes(self._config_path, defaults)

    def snapshot(self) -> QairtConfigSnapshot:
        """Capture the active config's exact bytes and configured SDK root."""

        raw = self._config_path.read_bytes()
        active_root = _parse_active_root(raw, source=self._config_path)
        return QairtConfigSnapshot(raw_bytes=raw, active_root=active_root)

    def resolve_target(self, install_root: Path, version: QairtVersion) -> Path:
        """Return an installed target directory constrained beneath ``install_root``."""
        resolved_target = _resolve_within_root(install_root / str(version), install_root)
        if not resolved_target.is_dir():
            raise QairtConfigPathError(f"{resolved_target} is not an existing directory")
        return resolved_target

    def activate(self, install_root: Path, version: QairtVersion) -> Path:
        """Point ``qairt_root`` at ``install_root / version`` and persist it.

        Validates that the target version directory exists and sits
        beneath ``install_root`` BEFORE touching the config file, so a
        rejected target never mutates the file or leaves a temp file
        behind. On success, canonical ``qairt_root`` is set and any stale
        legacy ``qairt_sdk_root`` is removed — the file never carries both
        keys. Every other field (``python_runtime_venv``,
        ``qairt_runtime_subdir``, ``_version``, and any unrecognised key)
        is preserved untouched. Returns the resolved, activated directory.
        """

        resolved_target = self.resolve_target(install_root, version)

        raw = self._config_path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise QairtConfigMalformedError(
                f"{self._config_path} is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise QairtConfigMalformedError(f"{self._config_path} does not contain a JSON object")

        payload.pop("qairt_sdk_root", None)
        payload["qairt_root"] = str(resolved_target)

        new_bytes = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        _atomic_write_bytes(self._config_path, new_bytes)
        return resolved_target

    def restore(self, snapshot: QairtConfigSnapshot) -> None:
        """Byte-exactly restore a previously captured :class:`QairtConfigSnapshot`."""

        _atomic_write_bytes(self._config_path, snapshot.raw_bytes)


__all__ = [
    "QairtConfigError",
    "QairtConfigMalformedError",
    "QairtConfigPathError",
    "QairtConfigRepository",
    "QairtConfigSnapshot",
]
