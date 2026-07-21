# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""KeyringSecretStore — OS keyring (Credential Manager / Keychain / Secret Service).

The :mod:`keyring` library exposes a uniform ``set_password`` /
``get_password`` / ``delete_password`` API but does **not** support
listing accounts under a service.  We therefore maintain a tiny sidecar
JSON index next to the keyring entries::

    <secrets_dir>/_index_<sanitized_service>.json
        {"keys": ["api_key", "refresh_token", ...]}

The index is updated atomically via :func:`tempfile.mkstemp` +
:func:`os.replace`.  The keyring remains the source of truth for the
*values*; the sidecar is treated as advisory metadata only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from qai.platform.config.paths import DataPaths
from qai.platform.errors import (
    ConfigurationError,
    InfrastructureError,
    NotFoundError,
)
from qai.platform.logging import get_logger

from .ports import assert_safe_namespace

_LOGGER = get_logger(__name__)

_INDEX_FILE_PREFIX: Final[str] = "_index_"
_INDEX_FILE_SUFFIX: Final[str] = ".json"


class KeyringSecretStore:
    """SecretStore adapter delegating to the OS keyring."""

    def __init__(self, *, data_paths: DataPaths) -> None:
        self._data_paths: DataPaths = data_paths
        try:
            import keyring as _keyring
            from keyring.errors import KeyringError  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised via factory
            raise ConfigurationError(
                "secrets.backend_unavailable",
                "keyring package is not installed; cannot use KeyringSecretStore",
            ) from exc
        self._keyring = _keyring

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        """Return True iff a *usable* keyring backend is available.

        ``keyring.backends.fail.Keyring`` is the sentinel "no usable
        backend" marker shipped by the keyring library itself; we
        explicitly treat it as unavailable.
        """
        try:
            import keyring as _keyring
            from keyring.backends.fail import Keyring as _FailKeyring
        except ImportError:
            return False
        try:
            backend = _keyring.get_keyring()
        except Exception:
            return False
        return not isinstance(backend, _FailKeyring)

    # ------------------------------------------------------------------
    # SecretStore protocol methods
    # ------------------------------------------------------------------

    def get(self, service: str, key: str) -> str:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        try:
            value = self._keyring.get_password(service, key)
        except self._keyring.errors.KeyringError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring error while reading {service}/{key}: {exc}",
            ) from exc
        if value is None:
            raise NotFoundError(
                "secrets.not_found", "secret", f"{service}/{key}"
            )
        return value

    def set(self, service: str, key: str, value: str) -> None:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        if not isinstance(value, str):
            raise TypeError("value must be str")
        try:
            self._keyring.set_password(service, key, value)
        except self._keyring.errors.KeyringError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring error while writing {service}/{key}: {exc}",
            ) from exc
        self._index_add(service, key)

    def delete(self, service: str, key: str) -> None:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        # Probe first so we can map "absent" -> NotFoundError.
        try:
            current = self._keyring.get_password(service, key)
        except self._keyring.errors.KeyringError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring error while probing {service}/{key}: {exc}",
            ) from exc
        if current is None:
            raise NotFoundError(
                "secrets.not_found", "secret", f"{service}/{key}"
            )
        try:
            self._keyring.delete_password(service, key)
        except self._keyring.errors.KeyringError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring error while deleting {service}/{key}: {exc}",
            ) from exc
        self._index_remove(service, key)

    def list_keys(self, service: str) -> list[str]:
        assert_safe_namespace(service)
        return sorted(self._index_load(service))

    def exists(self, service: str, key: str) -> bool:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        try:
            return self._keyring.get_password(service, key) is not None
        except self._keyring.errors.KeyringError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring error while checking {service}/{key}: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # Sidecar index file
    # ------------------------------------------------------------------

    def _index_path(self, service: str) -> Path:
        return self._data_paths.secret_file(
            f"{_INDEX_FILE_PREFIX}{service}{_INDEX_FILE_SUFFIX}"
        )

    def _index_load(self, service: str) -> set[str]:
        path = self._index_path(service)
        if not path.exists():
            return set()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"failed to read keyring index {path}: {exc}",
            ) from exc
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring index {path} is not valid JSON: {exc}",
            ) from exc
        if not isinstance(obj, dict):
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring index {path} has invalid schema (expected object)",
            )
        keys = obj.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise InfrastructureError(
                "secrets.corrupted",
                f"keyring index {path} has invalid 'keys' field",
            )
        return set(keys)

    def _index_save(self, service: str, keys: set[str]) -> None:
        self._data_paths.ensure(self._data_paths.secrets_dir)
        path = self._index_path(service)
        payload = json.dumps(
            {"keys": sorted(keys)}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        _atomic_write_bytes(path, payload)

    def _index_add(self, service: str, key: str) -> None:
        keys = self._index_load(service)
        if key in keys:
            return
        keys.add(key)
        self._index_save(service, keys)

    def _index_remove(self, service: str, key: str) -> None:
        keys = self._index_load(service)
        if key not in keys:
            return
        keys.discard(key)
        self._index_save(service, keys)


# ----------------------------------------------------------------------
# Module helper (no mutable state)
# ----------------------------------------------------------------------


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically write ``payload`` to ``path`` via tempfile + ``os.replace``."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                _LOGGER.warning("secrets.tempfile.cleanup_failed", path=str(tmp_path))
        raise


__all__ = ["KeyringSecretStore"]
