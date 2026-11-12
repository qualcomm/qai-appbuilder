# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SyncQaiServiceModelsUseCase`` — keep qai-service's model roster live.

QAI Service is opt-in: nothing is shipped. Once the user has entered the
gateway address (which creates an empty ``qai-service`` provider entry) and
signed in again (which mints the JWT), this use case probes the broker's
OpenAI-compatible ``GET /v1/models`` and folds what it returns into the
provider's stored ``models[]`` list, so the chat model dropdown can offer
them -- WITHOUT hardcoding a model list in source.

Design
------
* **The gateway is the only source of truth.** There is no built-in default
  model: a model is stored iff the live response lists it.
* **Dead models are removed** -- any stored model the live probe no longer
  returns is dropped on the next sync. A model that is both already stored AND
  still returned keeps 100% of its existing fields untouched.
* **New models get a minimal entry** -- the ``/v1/models`` response only ever
  gives an id, never a context window or sampling policy, so new entries use
  ``_DEFAULT_CONTEXT_LENGTH`` and an explicit unconstrained ``params``
  override (a model's real name, e.g. "qwen3.8-max", can collide with a family
  regex in ``qai.chat.domain.model_profiles`` and inherit an unrelated, usually
  lower, ``max_tokens`` ceiling purely by spelling).
* **Best-effort, never raises** -- every failure path (no JWT yet, no
  provider configured, probe failure) degrades to "do nothing, keep today's
  state".

Layer discipline
----------------
Stays in the *application* layer: imports only ports plus stdlib. The JWT
getter is injected as a plain callable; the composition root
(``apps/api/_model_catalog_di.py``) wires it.
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

#: Context window assumed for a model discovered via ``/v1/models`` (the
#: response carries none).
_DEFAULT_CONTEXT_LENGTH = 1_000_000

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

        An existing entry survives only if its ``api_model_id`` is still in
        ``live_ids``; every id in ``live_ids`` not yet represented becomes a
        new minimal entry.
        """
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        existing_by_api_id: dict[str, dict[str, Any]] = {}

        for raw in existing_models:
            if not isinstance(raw, dict):
                continue
            api_model_id = raw.get("api_model_id")
            if not isinstance(api_model_id, str) or not api_model_id:
                continue
            existing_by_api_id[api_model_id] = raw
            if api_model_id in live_ids:
                kept.append(raw)
            else:
                removed.append(api_model_id)

        added: list[str] = []
        new_entries: list[dict[str, Any]] = []
        for raw_id in live_ids:
            if raw_id in existing_by_api_id:
                continue
            new_entries.append(
                {
                    "model_id": f"{QAI_SERVICE_PROVIDER_ID}::{raw_id}",
                    "name": raw_id,
                    "api_model_id": raw_id,
                    "context_length": _DEFAULT_CONTEXT_LENGTH,
                    "description": "",
                    "supports_streaming": True,
                    "params": dict(_UNCONSTRAINED_PARAMS),
                }
            )
            added.append(raw_id)

        return [*kept, *new_entries], added, removed
