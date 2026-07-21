# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListCloudModelsUseCase`` -- flatten the cloud-inference model catalog.

Functional block 2 (cloud models real-availability). This use case is the
v2 equivalent of the legacy ``cloud_models.json::models`` list: it reads
the provider configs from :class:`ProviderRegistryPort` (KV-backed,
``model_catalog.provider.*``) and projects them into a flat, V1-style
list of cloud-inference models suitable for the chat model picker.

Distinction from :class:`ListModelEntriesUseCase`
--------------------------------------------------
``ListModelEntriesUseCase`` enumerates the **download catalog**
(``model_catalog_entry`` table) — entries that can be *fetched* to disk,
whose ``provider`` is the restricted :class:`ProviderKind` enum. That is
NOT the cloud-inference catalog the chat dropdown needs.

This use case enumerates **cloud-inference models** with a free-form
``provider`` string (``cloud_llm`` / ``provider_b`` / ...), exactly like
the legacy ``/api/models`` cloud merge. Pinned providers' models are
ordered first (legacy parity), then the rest; both keep provider-list
order otherwise.

Provider config shape consumed (per ``model_catalog.provider.<id>`` KV)::

    {
      "base_url": "https://...",          # provider endpoint (no secrets!)
      "pinned": false,                    # ordering hint
      "models": [                         # cloud-inference models
        {"model_id": "provider_id::model-name",
         "name": "claude-4-6-sonnet",
         "context_length": 200000,
         "description": "",
         "supports_streaming": true,
         "api_model_id": "claude-4-6-sonnet-20250514",  # optional wire override
         "params": {...}}                  # optional
      ]
    }

Credentials (api_key) are NEVER stored here; they live in the
``SecretStore`` and are injected by the downstream resolver. This use
case only surfaces the *catalog* (ids / names / context lengths), so the
list is safe to return unauthenticated like the legacy endpoint.
"""

from __future__ import annotations

from typing import Any

from qai.model_catalog.application.ports import ProviderRegistryPort


class ListCloudModelsUseCase:
    """Return a flat list of cloud-inference models (legacy parity)."""

    def __init__(self, *, registry: ProviderRegistryPort) -> None:
        self._registry = registry

    async def execute(self) -> list[dict[str, Any]]:
        provider_rows = await self._registry.list_provider_configs()

        pinned_models: list[dict[str, Any]] = []
        other_models: list[dict[str, Any]] = []

        for row in provider_rows:
            provider_id = row.get("provider_id")
            config = row.get("config")
            if not isinstance(provider_id, str) or not isinstance(config, dict):
                continue
            raw_models = config.get("models")
            if not isinstance(raw_models, list):
                continue
            is_pinned = bool(config.get("pinned", False))
            for raw in raw_models:
                model = self._project_model(raw, provider_id)
                if model is None:
                    continue
                if is_pinned:
                    pinned_models.append(model)
                else:
                    other_models.append(model)

        # Legacy parity: pinned providers first, then the rest.
        return pinned_models + other_models

    @staticmethod
    def _project_model(
        raw: Any, provider_id: str
    ) -> dict[str, Any] | None:
        """Project one stored model dict to the V1-style wire shape.

        Returns ``None`` for malformed rows (missing ``model_id``) so a
        single bad entry never breaks the whole list.
        """
        if not isinstance(raw, dict):
            return None
        model_id = raw.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            name = model_id
        context_length = raw.get("context_length")
        if not isinstance(context_length, int):
            context_length = None
        description = raw.get("description")
        if not isinstance(description, str):
            description = ""
        supports_streaming = raw.get("supports_streaming", True)
        if not isinstance(supports_streaming, bool):
            supports_streaming = True

        projected: dict[str, Any] = {
            "model_id": model_id,
            "name": name,
            "provider": provider_id,
            "context_length": context_length,
            "description": description,
            "supports_streaming": supports_streaming,
            "is_local": False,
        }
        # V1 ``api_model_id`` override (models_registry.py:76): the wire
        # model name sent to the provider's API when it differs from the
        # display ``model_id``. Surfaced only when present so the chat
        # picker / editor can round-trip it; absent keeps the JSON clean.
        wire = raw.get("api_model_id")
        if isinstance(wire, str) and wire:
            projected["api_model_id"] = wire
        params = raw.get("params")
        if isinstance(params, dict):
            projected["params"] = params
        return projected


__all__ = ["ListCloudModelsUseCase"]
