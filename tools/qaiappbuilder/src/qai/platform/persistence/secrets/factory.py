# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Factory + in-memory NullSecretStore.

:func:`build_secret_store` is the supported entry point for picking a
backend at process bootstrap.

:class:`NullSecretStore` is an in-memory adapter intended **for unit
tests only** — it never touches the filesystem or the OS keyring.
"""

from __future__ import annotations

import sys
from typing import Final, Literal

from qai.platform.config.paths import DataPaths
from qai.platform.errors import ConfigurationError, NotFoundError
from qai.platform.logging import get_logger

from .file_backend import FileSecretStore
from .keyring_backend import KeyringSecretStore
from .ports import SecretStore, assert_safe_namespace

_LOGGER = get_logger(__name__)

_PREFER_VALUES: Final[frozenset[str]] = frozenset({"keyring", "file", "auto"})


# ----------------------------------------------------------------------
# NullSecretStore — in-memory, tests only
# ----------------------------------------------------------------------


class NullSecretStore:
    """In-memory SecretStore. **Tests only — never use in production.**

    Holds records in an instance-level dict so multiple instances do not
    share state (no module-level mutable globals).
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def get(self, service: str, key: str) -> str:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        records = self._data.get(service, {})
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
        self._data.setdefault(service, {})[key] = value

    def delete(self, service: str, key: str) -> None:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        records = self._data.get(service, {})
        if key not in records:
            raise NotFoundError(
                "secrets.not_found", "secret", f"{service}/{key}"
            )
        del records[key]
        if not records:
            self._data.pop(service, None)

    def list_keys(self, service: str) -> list[str]:
        assert_safe_namespace(service)
        return sorted(self._data.get(service, {}).keys())

    def exists(self, service: str, key: str) -> bool:
        assert_safe_namespace(service)
        assert_safe_namespace(key)
        return key in self._data.get(service, {})


# ----------------------------------------------------------------------
# _FallbackSecretStore — keyring with transparent file-backend fallback
# ----------------------------------------------------------------------


class _FallbackSecretStore:
    """Try KeyringSecretStore first; fall back to FileSecretStore on write failure.

    Windows Credential Manager limits blobs to ~2560 bytes (ERROR_STUB_RECEIVED_BAD_DATA
    / CredWrite error 1783). OAuth JWT tokens often exceed this limit.  This wrapper
    transparently retries with the file backend on any write failure so that large
    credentials (tokens) are always persisted and survive process restarts.

    On read, both backends are checked: keyring first (small values such as API keys
    live there), then file (large values such as OAuth tokens).
    """

    def __init__(self, keyring: KeyringSecretStore, file: FileSecretStore) -> None:
        self._kr = keyring
        self._fl = file

    def get(self, service: str, key: str) -> str:
        try:
            return self._kr.get(service, key)
        except Exception:
            pass
        return self._fl.get(service, key)

    def set(self, service: str, key: str, value: str) -> None:
        kr_ok = False
        try:
            self._kr.set(service, key, value)
            kr_ok = True
        except Exception as exc:
            _LOGGER.debug(
                "secrets.keyring_set_failed_using_file service=%s key=%s: %s",
                service, key, exc,
            )
        # Always mirror to the file backend so that exists() / get() succeed
        # even when the keyring is available for writes but flaky on reads
        # (observed on arm64 Windows where keyring.get_password silently
        # raises in a new process after keyring.set_password succeeded).
        try:
            self._fl.set(service, key, value)
        except Exception as exc:
            if not kr_ok:
                raise  # Both backends failed; propagate to caller
            _LOGGER.debug(
                "secrets.file_mirror_failed service=%s key=%s: %s",
                service, key, exc,
            )

    def delete(self, service: str, key: str) -> None:
        kr_ok = True
        fl_ok = True
        try:
            self._kr.delete(service, key)
        except Exception:
            kr_ok = False
        try:
            self._fl.delete(service, key)
        except Exception:
            fl_ok = False
        if not kr_ok and not fl_ok:
            from qai.platform.errors import NotFoundError
            raise NotFoundError("secrets.not_found", "secret", f"{service}/{key}")

    def list_keys(self, service: str) -> list[str]:
        keys: set[str] = set()
        try:
            keys.update(self._kr.list_keys(service))
        except Exception:
            pass
        try:
            keys.update(self._fl.list_keys(service))
        except Exception:
            pass
        return sorted(keys)

    def exists(self, service: str, key: str) -> bool:
        try:
            if self._kr.exists(service, key):
                return True
        except Exception:
            pass
        # Keyring returned False or raised; also try get() in case exists()
        # is unreliable (e.g. _FallbackSecretStore on arm64 where the
        # keyring lib fails silently inside exists() but get() works).
        try:
            value = self._kr.get(service, key)
            if value:
                return True
        except Exception:
            pass
        try:
            return self._fl.exists(service, key)
        except Exception:
            return False


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

def build_secret_store(
    *,
    data_paths: DataPaths,
    prefer: Literal["keyring", "file", "auto"] = "auto",
) -> SecretStore:
    """Construct a :class:`SecretStore` according to ``prefer``.

    - ``"keyring"`` — force :class:`KeyringSecretStore`; raise
      :class:`ConfigurationError` if no usable keyring backend is
      available.
    - ``"file"`` — force :class:`FileSecretStore`; raise
      :class:`ConfigurationError` if the ``cryptography`` library is
      missing.
    - ``"auto"`` (default) — prefer keyring; fall back to file.
    """
    if prefer not in _PREFER_VALUES:
        raise ConfigurationError(
            "secrets.backend_unavailable",
            f"invalid prefer={prefer!r}; expected one of {sorted(_PREFER_VALUES)}",
        )

    if prefer == "keyring":
        if sys.platform == "linux":
            _LOGGER.warning(
                "secrets.keyring_forced_on_linux",
                warning="keyring forced on Linux; may fail in headless/server environments",
            )
        if not KeyringSecretStore.is_available():
            raise ConfigurationError(
                "secrets.backend_unavailable",
                "keyring backend requested but no usable keyring backend is available",
            )
        return KeyringSecretStore(data_paths=data_paths)

    if prefer == "file":
        if not FileSecretStore.is_available():
            raise ConfigurationError(
                "secrets.backend_unavailable",
                "file backend requested but 'cryptography' is not installed",
            )
        return FileSecretStore(data_paths=data_paths)

    # prefer == "auto"
    # On Linux (headless servers, SSH sessions, Docker containers), the OS
    # keyring (GNOME Keyring / Secret Service) may appear available via
    # is_available() but fail at runtime because the collection is locked
    # without a GUI session or D-Bus is absent.  Skip keyring entirely on
    # Linux and go straight to the file backend.
    if sys.platform == "linux":
        if FileSecretStore.is_available():
            _LOGGER.info(
                "secrets.backend.selected",
                backend="file",
                reason="linux_platform",
            )
            return FileSecretStore(data_paths=data_paths)
        raise ConfigurationError(
            "secrets.backend_unavailable",
            "file backend unavailable on Linux: 'cryptography' is not installed",
        )
    if KeyringSecretStore.is_available():
        if FileSecretStore.is_available():
            _LOGGER.info("secrets.backend.selected", backend="keyring+file_fallback")
            return _FallbackSecretStore(
                keyring=KeyringSecretStore(data_paths=data_paths),
                file=FileSecretStore(data_paths=data_paths),
            )
        _LOGGER.info("secrets.backend.selected", backend="keyring")
        return KeyringSecretStore(data_paths=data_paths)
    if FileSecretStore.is_available():
        _LOGGER.info("secrets.backend.selected", backend="file")
        return FileSecretStore(data_paths=data_paths)
    raise ConfigurationError(
        "secrets.backend_unavailable",
        "neither 'keyring' nor 'cryptography' is available; "
        "install one of them to use SecretStore",
    )


__all__ = ["NullSecretStore", "build_secret_store"]
