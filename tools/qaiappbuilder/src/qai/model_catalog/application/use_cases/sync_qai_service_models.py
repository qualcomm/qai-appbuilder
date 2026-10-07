# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SyncQaiServiceModelsUseCase`` — keep qai-service's model roster live.

Motivation
----------
``qai-service`` ships with exactly one configured model, the pseudo-model
``route-1`` (server-side "smart routing" — the broker itself picks the
underlying model). The broker also exposes its real, named models via a
standard OpenAI-compatible ``GET /v1/models``. This use case probes that
endpoint on every boot and folds any real models it finds into the
provider's stored ``models[]`` list, so the chat model dropdown can offer
them — WITHOUT ever hardcoding a model list in source.

Design
------
* **route-1 is sacred** — always kept, always first. The live
  ``/v1/models`` response may or may not even list ``route-1`` itself (it
  is a client-side convention for "let the broker choose", not necessarily
  a model the broker's own catalog advertises), so this is handled as an
  explicit special case rather than falling out of the general
  keep-if-still-returned rule.
* **Dead models are removed** — any previously-stored model (other than
  route-1) that the live probe no longer returns is dropped on the next
  sync. Models the user may have hand-edited (any field other than the
  identifying triplet) are only touched if removed entirely; a model that
  is both already-stored AND still returned keeps 100% of its existing
  fields untouched.
* **New models borrow route-1's effective parameters verbatim** — the
  ``/v1/models`` response only ever gives us an id, never a context
  window or sampling policy. Context length / supports_streaming are
  copied from route-1's own stored values. An explicit ``params`` override
  is ALSO attached (something route-1 itself does not carry — it does not
  need to, because its literal name "route-1" never collides with any
  ``qai.chat.domain.model_profiles`` family regex). A newly-discovered
  model's real name (e.g. "qwen3.8-max") very much CAN collide with one of
  those family patterns and inherit a materially different (usually
  lower) max_tokens ceiling purely by coincidence of spelling — the
  explicit override exists to pin sampling behaviour to what route-1
  already gets, regardless of what the model happens to be named.
* **Best-effort, never raises** — every failure path (no JWT yet, no
  provider configured, probe failure) degrades to "do nothing, keep
  today's state" so this is safe to fire from ``lifespan`` without any
  risk of blocking or crashing startup.

Layer discipline
----------------
Stays in the *application* layer like its sibling
``probe_cloud_model_permissions.py``: imports only ports
(``ProviderRegistryPort`` / ``ProviderProbePort``) plus stdlib. The JWT
getter is injected as a plain callable so this module never reaches into
``interfaces.http.auth`` directly — the composition root
(``apps/api/_model_catalog_di.py``) wires that callable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qai.model_catalog.application.ports import (
    ProviderProbePort,
    ProviderRegistryPort,
)
from qai.model_catalog.application.use_cases.list_provider_configs import (
    QAI_SERVICE_PROVIDER_ID,
)

__all__ = ["SyncQaiServiceModelsResult", "SyncQaiServiceModelsUseCase"]

#: The pseudo-model that must always survive a sync, regardless of whether
#: the live probe happens to list it.
_ROUTE1_API_MODEL_ID = "route-1"

#: Fallback shape for route-1 if it is ever missing from the stored config
#: (should not happen in practice — factory-seeded on every install — but a
#: sync must not be the thing that loses the default model).
_ROUTE1_FALLBACK_ENTRY: dict[str, Any] = {
    "model_id": "qai-service::route-1",
    "name": "Route 1",
    "api_model_id": "route-1",
    "context_length": 1_000_000,
    "description": "QAI Service 智能路由（自动选择底层模型）",
    "supports_streaming": True,
}

#: Sampling-parameter override attached to every newly-discovered model so
#: its behaviour matches route-1's (unconstrained) effective behaviour
#: regardless of what family `qai.chat.domain.model_profiles` would
#: otherwise match its name against. Shape consumed by
#: `qai.chat.infrastructure.model_param_resolver._constraint_from_config`.
_UNCONSTRAINED_PARAMS: dict[str, Any] = {
    "temperature": {"supported": True},
    "top_p": {"supported": True},
    "max_tokens": {"supported": True, "max": 1_000_000},
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncQaiServiceModelsResult:
    """Outcome of one ``SyncQaiServiceModelsUseCase.execute()`` call."""

    ok: bool = False
    added: tuple[str, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)
    skipped_reason: str | None = None

    def is_noop(self) -> bool:
        return not self.added and not self.removed


class SyncQaiServiceModelsUseCase:
    """Probe qai-service's live ``/v1/models`` and refresh its stored roster.

    Safe to call on every boot (and only on boot today — see the plan this
    shipped under): best-effort, idempotent, never raises.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        probe: ProviderProbePort,
        get_jwt: Callable[[], str | None],
    ) -> None:
        self._registry = registry
        self._probe = probe
        self._get_jwt = get_jwt

    async def execute(self) -> SyncQaiServiceModelsResult:
        try:
            jwt = self._get_jwt()
        except Exception:  # noqa: BLE001 — JWT resolution must never raise
            jwt = None
        if not jwt:
            return SyncQaiServiceModelsResult(ok=True, skipped_reason="no_jwt")

        try:
            config = await self._registry.get_provider_config(
                QAI_SERVICE_PROVIDER_ID
            )
        except Exception:  # noqa: BLE001 — registry failure is best-effort
            return SyncQaiServiceModelsResult(
                ok=False, skipped_reason="registry_error"
            )
        if config is None:
            return SyncQaiServiceModelsResult(
                ok=True, skipped_reason="provider_not_configured"
            )

        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            return SyncQaiServiceModelsResult(ok=True, skipped_reason="no_base_url")

        try:
            result = await self._probe.probe(base_url=base_url, api_key=jwt)
        except Exception:  # noqa: BLE001 — probe failure is best-effort
            return SyncQaiServiceModelsResult(ok=False, skipped_reason="probe_error")
        if not result.ok:
            return SyncQaiServiceModelsResult(ok=True, skipped_reason="probe_failed")

        existing_models = config.get("models")
        if not isinstance(existing_models, list):
            existing_models = []

        new_models, added, removed = self._merge(existing_models, result.model_ids)
        if not added and not removed:
            return SyncQaiServiceModelsResult(ok=True)

        new_config = dict(config)
        new_config["models"] = new_models
        try:
            await self._registry.save_provider_config(
                QAI_SERVICE_PROVIDER_ID, new_config
            )
        except Exception:  # noqa: BLE001 — save failure is best-effort
            return SyncQaiServiceModelsResult(ok=False, skipped_reason="save_error")

        return SyncQaiServiceModelsResult(
            ok=True, added=tuple(added), removed=tuple(removed)
        )

    @staticmethod
    def _merge(
        existing_models: list[Any], live_ids: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Return (new_models, added_ids, removed_ids).

        route-1 is always kept first, regardless of ``live_ids``. Every
        other existing entry survives only if its ``api_model_id`` is still
        in ``live_ids``; every id in ``live_ids`` not yet represented
        (and not "route-1") becomes a new minimal entry cloning route-1's
        context_length / supports_streaming / params.
        """
        route1_entry: dict[str, Any] | None = None
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        existing_by_api_id: dict[str, dict[str, Any]] = {}

        for raw in existing_models:
            if not isinstance(raw, dict):
                continue
            api_model_id = raw.get("api_model_id")
            if not isinstance(api_model_id, str) or not api_model_id:
                continue
            if api_model_id == _ROUTE1_API_MODEL_ID:
                route1_entry = raw
                continue
            existing_by_api_id[api_model_id] = raw
            if api_model_id in live_ids:
                kept.append(raw)
            else:
                removed.append(api_model_id)

        if route1_entry is None:
            route1_entry = dict(_ROUTE1_FALLBACK_ENTRY)

        added: list[str] = []
        new_entries: list[dict[str, Any]] = []
        for raw_id in live_ids:
            if raw_id == _ROUTE1_API_MODEL_ID or raw_id in existing_by_api_id:
                continue
            new_entries.append(
                {
                    "model_id": f"{QAI_SERVICE_PROVIDER_ID}::{raw_id}",
                    "name": raw_id,
                    "api_model_id": raw_id,
                    "context_length": route1_entry.get("context_length", 1_000_000),
                    "description": route1_entry.get("description", ""),
                    "supports_streaming": route1_entry.get(
                        "supports_streaming", True
                    ),
                    "params": dict(_UNCONSTRAINED_PARAMS),
                }
            )
            added.append(raw_id)

        new_models = [route1_entry, *kept, *new_entries]
        return new_models, added, removed
