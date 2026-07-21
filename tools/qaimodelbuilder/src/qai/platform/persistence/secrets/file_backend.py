# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""FileSecretStore — Fernet-encrypted on-disk credential store.

Layout
------
Each ``service`` namespace is one file under ``data_paths.secrets_dir``::

    <secrets_dir>/<sanitized_service>.bin     # Fernet-encrypted JSON {key: value}
    <secrets_dir>/_master.key                  # Fernet master key (fallback)

Master key resolution (highest priority first):

1. Constructor argument ``master_key`` (tests).
2. Environment variable ``QAI_SECRET_MASTER_KEY``.
3. Persistent file ``<secrets_dir>/_master.key``; auto-generated on
   first start.  POSIX hardens the file mode to ``0o600``; Windows
   relies on the parent ``<secrets_dir>`` being created inside the
   per-user ``%LOCALAPPDATA%`` tree (see :class:`DataPaths`), which
   already restricts access to the current user (Phase 8 cleanup
   removed the prior ``icacls`` belt-and-braces step).

All writes are atomic (``tempfile.NamedTemporaryFile`` + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import sys
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

_ENV_MASTER_KEY: Final[str] = "QAI_SECRET_MASTER_KEY"
_MASTER_KEY_FILENAME: Final[str] = "_master.key"
_SERVICE_FILE_SUFFIX: Final[str] = ".bin"


class FileSecretStore:
    """Encrypted-file SecretStore implementation.

    See module docstring for the on-disk layout and master-key
    resolution rules.
    """

    def __init__(
        self,
        *,
        data_paths: DataPaths,
        master_key: bytes | None = None,
    ) -> None:
        self._data_paths: DataPaths = data_paths
        self._explicit_master_key: bytes | None = master_key
        # Imported lazily so the module can be imported even if
        # ``cryptography`` is missing (the factory will detect that and
        # raise ConfigurationError).
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - exercised via factory
            raise ConfigurationError(
                "secrets.backend_unavailable",
                "cryptography package is not installed; cannot use FileSecretStore",
            ) from exc
        self._fernet_cls = Fernet
        self._fernet = Fernet(self._resolve_master_key())

    # ------------------------------------------------------------------
    # Availability probe (used by the factory)
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        """Return True iff ``cryptography.fernet`` can be imported."""
        try:
            from cryptography.fernet import Fernet  # noqa: F401
        except ImportError:
            return False
        return True

    # ------------------------------------------------------------------
    # SecretStore protocol methods
    # ------------------------------------------------------------------

    def get(self, service: str, key: str) -> str:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        records = self._read_records(service)
        if key not in records:
            raise NotFoundError(
                "secrets.not_found", "secret", f"{service}/{key}"
            )
        return records[key]

    def set(self, service: str, key: str, value: str) -> None:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        if not isinstance(value, str):
            raise TypeError("value must be str")
        records = self._read_records(service)
        records[key] = value
        self._write_records(service, records)

    def delete(self, service: str, key: str) -> None:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        records = self._read_records(service)
        if key not in records:
            raise NotFoundError(
                "secrets.not_found", "secret", f"{service}/{key}"
            )
        del records[key]
        self._write_records(service, records)

    def list_keys(self, service: str) -> list[str]:
        assert_safe_namespace(service)
        return sorted(self._read_records(service).keys())

    def exists(self, service: str, key: str) -> bool:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        return key in self._read_records(service)

    # ------------------------------------------------------------------
    # Internals — master key
    # ------------------------------------------------------------------

    def _resolve_master_key(self) -> bytes:
        if self._explicit_master_key is not None:
            return self._explicit_master_key
        env_value = os.environ.get(_ENV_MASTER_KEY)
        if env_value:
            return env_value.encode("ascii")
        return self._load_or_create_master_key_file()

    def _load_or_create_master_key_file(self) -> bytes:
        path = self._data_paths.secret_file(_MASTER_KEY_FILENAME)
        if path.exists():
            try:
                return path.read_bytes().strip()
            except OSError as exc:
                raise InfrastructureError(
                    "secrets.corrupted",
                    f"failed to read master key {path}: {exc}",
                ) from exc

        # First start — generate and persist a new key.
        self._data_paths.ensure(self._data_paths.secrets_dir)
        new_key = self._fernet_cls.generate_key()
        _atomic_write_bytes(path, new_key)
        # Harden the master-key file permissions to owner-only on POSIX
        # via ``chmod 0o600``. Windows is a no-op: the secrets directory
        # is created under per-user ``%LOCALAPPDATA%`` (see DataPaths),
        # whose default ACL already restricts access to the current
        # user; the previous belt-and-braces ``icacls`` step was removed
        # in Phase 8 cleanup (2026-07-01).
        _harden_master_key_acl(path)
        _LOGGER.info("secrets.master_key.generated", path=str(path))
        return new_key

    # ------------------------------------------------------------------
    # Internals — encrypted record I/O
    # ------------------------------------------------------------------

    def _service_path(self, service: str) -> Path:
        return self._data_paths.secret_file(f"{service}{_SERVICE_FILE_SUFFIX}")

    def _read_records(self, service: str) -> dict[str, str]:
        path = self._service_path(service)
        if not path.exists():
            return {}
        try:
            ciphertext = path.read_bytes()
        except OSError as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"failed to read secrets file {path}: {exc}",
            ) from exc
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except Exception as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"failed to decrypt secrets file {path}: {exc}",
            ) from exc
        try:
            obj = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfrastructureError(
                "secrets.corrupted",
                f"secrets file {path} contains invalid JSON: {exc}",
            ) from exc
        if not isinstance(obj, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in obj.items()
        ):
            raise InfrastructureError(
                "secrets.corrupted",
                f"secrets file {path} has invalid schema",
            )
        return obj

    def _write_records(self, service: str, records: dict[str, str]) -> None:
        self._data_paths.ensure(self._data_paths.secrets_dir)
        path = self._service_path(service)
        plaintext = json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        _atomic_write_bytes(path, ciphertext)


# ----------------------------------------------------------------------
# Module-level helper (no mutable state)
# ----------------------------------------------------------------------


def _harden_master_key_acl(path: Path) -> None:
    """Best-effort owner-only hardening of the master-key file.

    * POSIX: ``os.chmod(path, 0o600)`` — owner read/write only.
    * Windows: **no-op**. The master key lives under
      ``%LOCALAPPDATA%\\QAIModelBuilder\\...\\secrets\\``; the
      per-user ``%LOCALAPPDATA%`` ACL already restricts access to the
      current user (the directory is created by Windows in a tree only
      the current user and SYSTEM can read by default), so we rely on
      that inherited ACL rather than running ``icacls`` here.

      The previous Windows branch invoked ``icacls /inheritance:r
      /grant:r <user>:F`` to strip inherited ACEs as belt-and-braces
      defence. Phase 8 cleanup (2026-07-01) removed it alongside the
      rest of the Windows AppContainer / Win32-ACL surface: ``icacls``
      cannot easily be sandboxed, the parent ACL is sufficient, and
      keeping the path obviates the need to special-case ``icacls``
      failure modes (missing binary, locked file, antivirus
      interference) on Windows.

    POSIX failure is graceful — a chmod error only emits a warning so
    first-start key generation never crashes; the file inherits the
    process umask in that rare case.
    """
    if sys.platform == "win32":
        # Rely on the per-user %LOCALAPPDATA% ACL inherited by the
        # secrets directory (see DataPaths). No-op here.
        return
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best-effort
        _LOGGER.warning(
            "secrets.master_key.chmod_failed",
            path=str(path),
        )


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


__all__ = ["FileSecretStore"]
