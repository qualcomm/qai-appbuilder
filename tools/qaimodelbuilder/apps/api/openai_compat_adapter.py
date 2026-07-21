# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Default :class:`OpenAIModelListerPort` adapter (PR-042 / issue b).

Composes data from two contexts via their existing **read** use cases
without violating ``context-isolation`` (this module lives at the apps
layer, the only place where cross-context composition is allowed):

* ``container.model_catalog.list_model_entries_use_case`` -- enumerates
  registered model entries;
* a static seed list derived from ``container.settings`` -- ensures
  ``/v1/models`` is non-empty even on a freshly-installed system so
  the OpenAI Python SDK doesn't fail discovery.

The adapter is read-only and stateless: every call re-runs the
underlying use case, so the OpenAI HTTP layer never serves a stale
snapshot after the catalogue is mutated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .openai_compat_ports import OpenAIModelInfo, OpenAIModelListerPort

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = ["DefaultOpenAIModelListerAdapter"]


# Static seed entries so the directory is never empty. Mirror PR-033's
# placeholder list to preserve the existing wire shape (``qai-default``
# / ``qai-fast`` were the static fakes before issue b was resolved).
_SEED_MODELS: tuple[tuple[str, str], ...] = (
    ("qai-default", "qai"),
    ("qai-fast", "qai"),
)


class DefaultOpenAIModelListerAdapter:
    """Apps-layer adapter wiring two contexts behind one read facade.

    Construction takes the ``Container`` so the adapter can call the
    relevant use cases at request time -- this avoids capturing stale
    references and keeps the implementation symmetric with the other
    apps-layer adapters (e.g. ``SystemRebootSignalAdapter``).
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    async def list_models(self) -> list[OpenAIModelInfo]:
        clock = self._container.clock
        now_unix = int(clock.now().timestamp())
        results: list[OpenAIModelInfo] = []
        seen: set[str] = set()

        # Catalogue entries first (real data wins over seed).
        try:
            entries = await (
                self._container.model_catalog.list_model_entries_use_case.execute()
            )
        except Exception:  # noqa: BLE001 -- adapter contract: never raise
            entries = []
        for entry in entries:
            model_id = entry.model_id.value
            if model_id in seen:
                continue
            seen.add(model_id)
            results.append(
                OpenAIModelInfo(
                    id=model_id,
                    created=now_unix,
                    owned_by="qai",
                )
            )

        # Seed entries close any gap so the directory is never empty.
        for model_id, owner in _SEED_MODELS:
            if model_id in seen:
                continue
            seen.add(model_id)
            results.append(
                OpenAIModelInfo(
                    id=model_id,
                    created=now_unix,
                    owned_by=owner,
                )
            )
        return results

    async def get_model(self, model_id: str) -> OpenAIModelInfo | None:
        for info in await self.list_models():
            if info.id == model_id:
                return info
        return None
