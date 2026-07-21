# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SecretStore-backed :class:`CredentialsResolverPort` (PR-047).

Adapter wrapping :class:`qai.platform.persistence.secrets.SecretStore` so
``CredentialsRef -> str`` resolution goes through the OS keyring (with
Fernet-encrypted file fallback) rather than plaintext disk storage.

The adapter is the *only* seam at which channels code touches
:class:`SecretStore`; use cases / domain code never imports
:mod:`qai.platform.persistence.secrets`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qai.platform.errors import NotFoundError, PersistenceError

from qai.channels.domain import CredentialsNotFoundError, CredentialsRef

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

__all__ = ["SecretStoreCredentialsResolver"]


class SecretStoreCredentialsResolver:
    """:class:`CredentialsResolverPort` adapter over :class:`SecretStore`.

    ``SecretStore`` operations are synchronous (the keyring / file
    backends both block); we wrap each call in
    :func:`asyncio.to_thread` so we don't pin the event loop while
    Windows Credential Manager / macOS Keychain authenticates the user.
    """

    __slots__ = ("_store",)

    def __init__(self, *, store: "SecretStore") -> None:
        self._store = store

    async def resolve(self, ref: CredentialsRef) -> str:
        try:
            return await asyncio.to_thread(
                self._store.get, ref.service, ref.key
            )
        except NotFoundError as exc:
            raise CredentialsNotFoundError(
                f"{ref.service}:{ref.key}"
            ) from exc
        except ValueError as exc:
            # Invalid namespace / key — surface as a credentials issue
            # rather than a generic ValueError so the route layer keeps
            # the unified envelope.
            raise CredentialsNotFoundError(
                f"{ref.service}:{ref.key}",
                message=f"invalid credentials ref: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.credentials.resolve_failed",
                f"failed to resolve credentials "
                f"{ref.service}:{ref.key}: {exc}",
                operation="channels.credentials.resolve",
                cause=exc,
            ) from exc

    async def store(self, ref: CredentialsRef, secret: str) -> None:
        try:
            await asyncio.to_thread(
                self._store.set, ref.service, ref.key, secret
            )
        except ValueError as exc:
            raise CredentialsNotFoundError(
                f"{ref.service}:{ref.key}",
                message=f"invalid credentials ref: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.credentials.store_failed",
                f"failed to store credentials "
                f"{ref.service}:{ref.key}: {exc}",
                operation="channels.credentials.store",
                cause=exc,
            ) from exc
