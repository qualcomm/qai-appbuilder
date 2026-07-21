# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Network-proxy settings use cases (R4/R5).

The ``GET/POST /api/proxy`` endpoints persist the proxy **username** into
``forge.config network_proxy.proxy_username`` and the **password** into the
:class:`SecretStore` (AGENTS.md §3.3 — credentials never enter the KV
document).

Proxy **URL** ownership (2026-07 unification)
---------------------------------------------
The proxy URL is NOT stored under ``network_proxy.proxy_url`` anymore — that
field was write-only/dead (no runtime reader). The single URL truth every
outbound client reads is ``security_runtime_config.global_proxy`` (written by
the ``PUT /api/security/runtime-config`` route, which also hot-applies it).
So:

* :class:`GetProxyUseCase` returns ``proxy_url`` READ FROM
  ``security_runtime_config.global_proxy`` (so the channel "sync global proxy"
  button and any GET consumer see the real, live URL).
* :class:`SaveProxyUseCase` writes ONLY username (KV) + password (SecretStore);
  it no longer writes ``network_proxy.proxy_url`` (the URL is saved via the
  runtime-config route). This removes the split-brain where the URL box in one
  panel was inert.

Pulling this read-modify-write + masked-password handling out of the route
keeps the route declarative and the credential-masking policy in the
application layer where it can be unit-tested without a live FastAPI app.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qai.platform.persistence.secrets import SecretStore
from qai.user_prefs.application.use_cases.load_document import (
    LoadDocumentUseCase,
)
from qai.user_prefs.application.use_cases.save_document import (
    SaveDocumentUseCase,
)

__all__ = ["GetProxyUseCase", "SaveProxyUseCase"]

_NETWORK_PROXY_SUBKEY = "network_proxy"
# The live proxy-URL truth source read by every outbound client (via
# ``settings.tools.global_proxy``). Persisted under this forge_config region by
# the runtime-config route. Mirrors ``apps/api/_runtime_config_store.py``
# ``RUNTIME_CONFIG_KEY`` / ``global_proxy`` field.
_RUNTIME_CONFIG_SUBKEY = "security_runtime_config"
_GLOBAL_PROXY_FIELD = "global_proxy"


def _coerce_section(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


@dataclass(slots=True, frozen=True)
class GetProxyUseCase:
    """Return the persisted proxy URL/username + a masked password flag.

    The password value is never returned: only the mask string (when a
    password exists in the SecretStore) or empty string, mirroring the
    legacy GET handler exactly.
    """

    load_document_use_case: LoadDocumentUseCase
    secret_store: SecretStore
    forge_config_key: str
    secret_service: str
    secret_key: str
    mask: str

    async def execute(self) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        section = _coerce_section(doc.get(_NETWORK_PROXY_SUBKEY, {}))
        # URL comes from the live global_proxy truth (security_runtime_config),
        # NOT the dead network_proxy.proxy_url — so every consumer sees the same
        # URL the outbound clients actually use.
        runtime_section = _coerce_section(doc.get(_RUNTIME_CONFIG_SUBKEY, {}))
        proxy_url = str(runtime_section.get(_GLOBAL_PROXY_FIELD, "") or "")
        has_password = self.secret_store.exists(
            self.secret_service, self.secret_key
        )
        return {
            "proxy_url": proxy_url,
            "proxy_username": str(section.get("proxy_username", "")),
            "proxy_password": self.mask if has_password else "",
        }


@dataclass(slots=True, frozen=True)
class SaveProxyUseCase:
    """Persist proxy URL/username (KV) + password (SecretStore).

    Password semantics (legacy parity):

    * value == ``mask`` → user did not change it → skip write;
    * value == ``""``   → user cleared it → delete from SecretStore;
    * otherwise         → new password → store it.
    """

    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    secret_store: SecretStore
    forge_config_key: str
    secret_service: str
    secret_key: str
    mask: str

    async def execute(self, body: Mapping[str, Any]) -> dict[str, Any]:
        proxy_username = str(body.get("proxy_username", ""))
        proxy_password = str(body.get("proxy_password", ""))

        # Persist ONLY the username here. The proxy URL is owned by the
        # runtime-config route (security_runtime_config.global_proxy); writing
        # network_proxy.proxy_url would recreate the dead/duplicate field. Any
        # ``proxy_url`` in the body is intentionally ignored.
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        existing = _coerce_section(doc.get(_NETWORK_PROXY_SUBKEY, {}))
        existing["proxy_username"] = proxy_username
        # Drop a previously-persisted dead URL so the document self-heals.
        existing.pop("proxy_url", None)
        await self.save_document_use_case.execute(
            self.forge_config_key, updates={_NETWORK_PROXY_SUBKEY: existing}
        )

        if proxy_password == self.mask:
            # User didn't change the password — skip write.
            pass
        elif proxy_password == "":
            # User cleared the password — delete from SecretStore.
            if self.secret_store.exists(self.secret_service, self.secret_key):
                self.secret_store.delete(self.secret_service, self.secret_key)
        else:
            # New password provided — save to SecretStore.
            self.secret_store.set(
                self.secret_service, self.secret_key, proxy_password
            )

        return {"success": True}
