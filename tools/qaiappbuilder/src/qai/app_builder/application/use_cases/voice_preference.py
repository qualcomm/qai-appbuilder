# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Voice input preference use cases (get / set).

Both use cases are thin wrappers around
:class:`VoiceInputPreferenceRepositoryPort` but live in the application
layer so the interfaces layer never imports the port directly (keeping
the ``layered-app_builder`` import-linter contract simple to enforce).
"""

from __future__ import annotations

from qai.app_builder.application.ports import (
    VoiceInputPreferenceRepositoryPort,
)
from qai.app_builder.domain.errors import VoicePreferenceInvalidError
from qai.app_builder.domain.value_objects import AppModelId
from qai.app_builder.domain.voice_preference import VoiceInputPreference

__all__ = [
    "GetVoicePreferenceUseCase",
    "SetVoicePreferenceUseCase",
]


class GetVoicePreferenceUseCase:
    """Return the current voice preference, falling back to the default."""

    def __init__(
        self, *, prefs: VoiceInputPreferenceRepositoryPort
    ) -> None:
        self._prefs = prefs

    async def execute(self) -> VoiceInputPreference:
        return await self._prefs.get()


class SetVoicePreferenceUseCase:
    """Validate and persist a new voice preference.

    Raw inputs (``enabled: bool``, ``preferred_model_id: str | None``)
    are accepted to match the legacy HTTP request shape; we wrap them
    into the :class:`VoiceInputPreference` VO ourselves so the
    interfaces layer doesn't need to know about VO construction.
    """

    def __init__(
        self, *, prefs: VoiceInputPreferenceRepositoryPort
    ) -> None:
        self._prefs = prefs

    async def execute(
        self,
        *,
        enabled: bool,
        preferred_model_id: str | None,
        preferred_variant_id: str | None = None,
    ) -> VoiceInputPreference:
        try:
            model_id_vo: AppModelId | None
            if preferred_model_id is None:
                model_id_vo = None
            else:
                model_id_vo = AppModelId(value=preferred_model_id)
            pref = VoiceInputPreference(
                enabled=enabled,
                preferred_model_id=model_id_vo,
                # V1 parity: remember the exact variant the user picked so the
                # warm-up loads that variant (not just the model). Tail-append
                # optional (v2.7 §3.1) — older callers omit it → None → the
                # adapter resolves a default variant.
                preferred_variant_id=preferred_variant_id,
            )
        except ValueError as exc:
            raise VoicePreferenceInvalidError(
                message=str(exc),
                details={
                    "enabled": enabled,
                    "preferred_model_id": preferred_model_id,
                    "preferred_variant_id": preferred_variant_id,
                },
                cause=exc,
            ) from exc
        await self._prefs.set(pref)
        return pref
