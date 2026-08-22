# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Secret / credential storage adapters.

This subpackage replaces the legacy plaintext ``data/wechat_creds.json``
file (see inventory ``05-data-config.md`` / ``09-final-delete-list.md``
§11) with a port-driven :class:`SecretStore`:

- :class:`KeyringSecretStore` — OS keyring (preferred).
- :class:`FileSecretStore`    — Fernet-encrypted on-disk fallback.
- :class:`NullSecretStore`    — in-memory, **tests only**.
- :func:`build_secret_store`  — factory selecting a backend.

Public API
----------
::

    from qai.platform.persistence.secrets import (
        SecretStore,
        KeyringSecretStore,
        FileSecretStore,
        NullSecretStore,
        build_secret_store,
    )
"""

from __future__ import annotations

from .factory import NullSecretStore, build_secret_store
from .file_backend import FileSecretStore
from .keyring_backend import KeyringSecretStore
from .ports import SecretStore

__all__ = [
    "SecretStore",
    "KeyringSecretStore",
    "FileSecretStore",
    "NullSecretStore",
    "build_secret_store",
]
