# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``VoiceInputPreference`` value object.

Models the legacy ``data/app_builder/voice_input_pref.json`` content
(see ``05-data-config.md`` line 92). Three leaf settings:

* :attr:`enabled` — whether voice-input UI is shown at all;
* :attr:`preferred_model_id` — optional :class:`AppModelId` of the
  speech-recognition model to use; ``None`` means "let the adapter pick
  a default".
* :attr:`preferred_variant_id` — optional variant id (PR-307 / multi-variant
  pack contract §3); pinned alongside ``preferred_model_id`` so the
  warm-up loads exactly the variant the user prefers. ``None`` means
  "let the adapter resolve via :func:`select_variant`".

The VO has no behaviour beyond input validation; persistence is done
by an adapter through the :class:`VoiceInputPreferenceRepositoryPort`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from qai.app_builder.domain.value_objects import AppModelId

__all__ = ["VoiceInputPreference"]


# Same character set as VariantSpec.id (multi-variant-pack-contract §1.4)
_VARIANT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceInputPreference:
    """User-scoped voice input preference."""

    enabled: bool
    preferred_model_id: AppModelId | None = None
    # PR-307 — tail-append. Existing PR-045 callers that construct
    # ``VoiceInputPreference(enabled=...)`` continue to work because the
    # new field defaults to ``None``.
    preferred_variant_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError(
                "VoiceInputPreference.enabled must be bool, "
                f"got {type(self.enabled).__name__}"
            )
        if self.preferred_model_id is not None and not isinstance(
            self.preferred_model_id, AppModelId
        ):
            raise ValueError(
                "VoiceInputPreference.preferred_model_id must be AppModelId or None, "
                f"got {type(self.preferred_model_id).__name__}"
            )
        if self.preferred_variant_id is not None:
            if not isinstance(self.preferred_variant_id, str):
                raise ValueError(
                    "VoiceInputPreference.preferred_variant_id must be str or None, "
                    f"got {type(self.preferred_variant_id).__name__}"
                )
            if not _VARIANT_ID_RE.match(self.preferred_variant_id):
                raise ValueError(
                    "VoiceInputPreference.preferred_variant_id must match "
                    f"[A-Za-z0-9_.-]{{1,64}}, got {self.preferred_variant_id!r}"
                )
            # Cross-field invariant: variant_id only meaningful with a model_id.
            if self.preferred_model_id is None:
                raise ValueError(
                    "VoiceInputPreference.preferred_variant_id requires "
                    "preferred_model_id to also be set"
                )

    @classmethod
    def default(cls) -> VoiceInputPreference:
        """Return the documented default (enabled, Zipformer Chinese int8).

        2026-06-21 product decision (AGENTS.md "用户拍板"): first launch ships
        with voice input *enabled* + Zipformer-zh (int8) as the preferred
        engine, so users can click the mic immediately without first opening
        the engine popover. This pairs with the frontend's removal of the
        ``localStorage`` engineId mirror — DB is now the single source of
        truth for ``enabled / preferred_model_id / preferred_variant_id``,
        and the UI derives the selected engine from this default on first
        boot.
        """
        return cls(
            enabled=True,
            preferred_model_id=AppModelId(value="zipformer-zh"),
            preferred_variant_id="int8",
        )
