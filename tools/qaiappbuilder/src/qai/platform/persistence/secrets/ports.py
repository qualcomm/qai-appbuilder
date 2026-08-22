# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SecretStore port — the abstract interface for credential storage.

Adapters implementing :class:`SecretStore` must persist ``(service, key) -> str``
pairs in a way that is at least as durable as the OS keyring or an
encrypted on-disk file.

Design notes
------------
- The store is **namespace-scoped** by ``service`` (e.g.
  ``"qai.channels.wechat"``).  Two services with the same ``key`` are
  independent records.
- ``value`` is always a plain ``str``.  If callers need to store binary
  blobs, they must base64-encode them first.
- ``service`` and ``key`` MUST pass
  :func:`qai.platform.io_validator.assert_safe_filename` before reaching
  the adapter — every adapter re-validates defensively to refuse path
  traversal injection into keyring service names or filenames.
- ``list_keys(service)`` returns only the **keys**; values are never
  exposed in bulk to discourage misuse (e.g. logging a dump).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
)
from qai.platform.io_validator import (
    assert_safe_filename as _assert_safe_filename,
)


def assert_safe_namespace(name: str) -> None:
    """Validate a ``service`` or ``key`` for use in any SecretStore backend.

    Wraps :func:`qai.platform.io_validator.assert_safe_filename` so that
    path-traversal / NUL / Windows-reserved-name violations surface as
    plain :class:`ValueError` to the caller — matching the contract
    documented on :class:`SecretStore`.
    """
    if not isinstance(name, str):
        raise ValueError(f"name must be str, got {type(name).__name__}")
    try:
        _assert_safe_filename(name)
    except _IoValidationError as exc:
        raise ValueError(str(exc)) from exc


@runtime_checkable
class SecretStore(Protocol):
    """Persistent credential store, namespaced by ``service``.

    Implementations:

    - :class:`qai.platform.persistence.secrets.KeyringSecretStore` —
      OS keyring backed (Windows Credential Manager / macOS Keychain /
      Linux Secret Service).
    - :class:`qai.platform.persistence.secrets.FileSecretStore` —
      ``cryptography.fernet`` encrypted file fallback.
    - :class:`qai.platform.persistence.secrets.NullSecretStore` —
      in-memory, **for tests only**.
    """

    def get(self, service: str, key: str) -> str:
        """Return the stored value.

        Raises:
            qai.platform.errors.NotFoundError: if no record exists.
            ValueError: if ``service`` / ``key`` is not a safe filename.
        """
        ...

    def set(self, service: str, key: str, value: str) -> None:
        """Insert or overwrite a value.

        Raises:
            ValueError: if ``service`` / ``key`` is not a safe filename.
        """
        ...

    def delete(self, service: str, key: str) -> None:
        """Remove a record.

        Raises:
            qai.platform.errors.NotFoundError: if no record exists.
            ValueError: if ``service`` / ``key`` is not a safe filename.
        """
        ...

    def list_keys(self, service: str) -> list[str]:
        """Return the keys (not values) currently stored under ``service``.

        Returns an empty list if the namespace has no records.
        """
        ...

    def exists(self, service: str, key: str) -> bool:
        """Return ``True`` iff a record for ``(service, key)`` exists."""
        ...


__all__ = ["SecretStore", "assert_safe_namespace"]
