# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Display-name + pack-id derivation helpers.

Direct port of the four ``infer_*`` helpers from
``features/model-builder/scripts/qai_pack_export.py``:

* :func:`infer_pack_id_no_precision` — multi-variant canonical id
  (``model_name`` only, no precision suffix);
* :func:`infer_pack_id` — legacy single-variant id with precision;
* :func:`infer_display_name_no_precision` — title-cased model name
  for multi-variant Packs (precision is shown via VariantSwitcher);
* :func:`infer_display_name` — single-variant display string with a
  ``(PRECISION)`` suffix.
"""

from __future__ import annotations

__all__ = [
    "infer_pack_id_no_precision",
    "infer_pack_id",
    "infer_display_name_no_precision",
    "infer_display_name",
]


def infer_pack_id_no_precision(model_name: str) -> str:
    """Multi-variant canonical pack id: ``model_name`` only.

    Per the multi-variant Pack contract, ``modelId`` is the *logical*
    model identifier and is not tied to any single precision; each
    precision is exposed as a ``variants[i]`` entry instead.
    """
    return (model_name or "").lower().replace("_", "-")


def infer_pack_id(model_name: str, precision_label: str) -> str:
    """Legacy single-variant pack id with precision suffix.

    Kept for the backwards-compatible single-variant code path only.
    Multi-variant callers must use :func:`infer_pack_id_no_precision`.
    """
    base = (model_name or "").lower().replace("_", "-")
    return f"{base}-{(precision_label or '').lower()}"


def infer_display_name_no_precision(model_name: str) -> str:
    """Title-cased display name without a precision suffix.

    Used for multi-variant Packs where the active precision is
    conveyed by the VariantSwitcher chip in the App Builder Workbench
    Header rather than by the model name itself.
    """
    parts = (model_name or "").replace("-", "_").split("_")
    titled: list[str] = []
    for p in parts:
        if (
            p.lower().startswith("v")
            and len(p) <= 4
            and any(c.isdigit() for c in p)
        ):
            titled.append(p)  # keep version like v3, v8n
        else:
            titled.append(p.capitalize())
    return " ".join(titled)


def infer_display_name(model_name: str, precision_label: str) -> str:
    """Title-cased display name with a ``(PRECISION)`` suffix.

    Used only by the legacy single-variant code path. Multi-variant
    callers must use :func:`infer_display_name_no_precision`.
    """
    base = infer_display_name_no_precision(model_name)
    return f"{base} ({(precision_label or '').upper()})"
