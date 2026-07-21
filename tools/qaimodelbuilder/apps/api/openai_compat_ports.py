# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer ports for the OpenAI-compatible HTTP surface (PR-042).

Issue (b) resolution from ``HANDOFF-after-PR-040.md`` §5: ``/v1/models``
needs to surface real catalogue data without violating
``context-isolation`` (chat must NOT import from ``model_catalog``).
The decision (PR-040 manifest §11 row 2 option B) is to introduce a
small port at the **apps layer** -- not inside any context -- and have
``apps.api.di`` wire its default adapter by combining data from
``container.model_catalog.list_model_entries_use_case`` and
``container.chat.llm`` (registered providers).

Routes consume this port via ``container.openai_compat.model_lister``
(read-only, no domain logic), bypassing the cross-context import the
import-linter would otherwise reject.

Why apps-layer instead of ``qai.platform.*``?
---------------------------------------------
The composition is *application-level* coordination -- it merges two
contexts' read paths into one HTTP-shaped projection. Promoting the
port to ``qai.platform.*`` would require a shared kernel API for
"models", which is overkill: the apps layer is the canonical location
for cross-context HTTP-shaped read facades (per `S3-sub-agent-spec.md`
§8 row 6 + PR-040 manifest §10.2 wiring guidance).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


__all__ = [
    "OpenAIModelInfo",
    "OpenAIModelListerPort",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIModelInfo:
    """One row in the OpenAI ``/v1/models`` projection.

    Apps-layer DTO -- intentionally small and HTTP-shaped so the route
    layer can serialise it without further mapping. Fields mirror the
    OpenAI API contract (``id`` / ``created`` / ``owned_by``).
    """

    id: str
    created: int
    owned_by: str = "qai"


@runtime_checkable
class OpenAIModelListerPort(Protocol):
    """Port surfacing the merged model directory for ``/v1/models``.

    The default adapter (``apps.api.openai_compat_adapter``) combines:

    * every :class:`qai.model_catalog.domain.entities.ModelEntry` from
      ``container.model_catalog.list_model_entries_use_case``;
    * one synthetic entry per chat-context provider configured in
      ``container.settings.chat`` (e.g. an ``"openai"`` baseline entry
      so the OpenAI client sees at least one model id even when the
      catalogue is empty).

    The port is intentionally tiny -- no filtering / pagination -- so
    ``/v1/models`` and ``/v1/models/{id}`` can serve from a single
    snapshot per request without further round-trips.
    """

    async def list_models(self) -> list[OpenAIModelInfo]:
        """Return the current model directory."""
        ...

    async def get_model(self, model_id: str) -> OpenAIModelInfo | None:
        """Return one entry, or ``None`` if the id is unknown."""
        ...
