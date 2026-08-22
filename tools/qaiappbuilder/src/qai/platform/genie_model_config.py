# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared single-model config file resolution (Genie SDK naming parity).

Newer Qualcomm AI Hub exports (``qairt`` bundles, e.g. Qwen3-VL)
ship ``genie_config.json`` instead of the ``config.json`` name used by the
models installed to date, so both names must resolve.

The GenieAPIService binary resolves the same two names itself: the exe
shipped in the v2.48.40 package (``GenieService-win-arm64.zip``) carries
both ``/genie_config.json`` and ``/config.json`` probe strings, whereas the
older v2.3.7 build carries only ``/config.json``.
:func:`resolve_model_config_path` mirrors that probe so this side agrees
with whichever daemon build is installed — and because the ``-c`` argument
we pass is an explicit full path, the older daemon loads a
``genie_config.json`` model too.

Keep the order (``genie_config.json`` first) aligned with the daemon: for a
model dir holding BOTH files, a divergent order would launch the model with
one config while the daemon's own directory scan reads the other, silently
applying mismatched settings.

The helper lives in the ``qai.platform`` shared kernel, not
``qai.model_runtime`` or ``qai.service_release``, because the
``layered-tools`` ``.importlinter`` contract forbids those two contexts
from importing each other while explicitly allowing every context to use
``qai.platform`` — this is the only place both call sites (model launch in
``model_runtime`` and install/scan in ``service_release``) can share it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_model_config_path"]

#: Probe order mirrors the GenieAPIService daemon exactly: the newer Genie
#: SDK naming wins when both files are present.
_CONFIG_FILENAMES = ("genie_config.json", "config.json")


def resolve_model_config_path(model_dir: Path) -> Path | None:
    """Return the single-model config file under ``model_dir``, or ``None``.

    Probes ``genie_config.json`` first, then ``config.json``; returns
    ``None`` when neither exists so callers can degrade gracefully (skip
    the model / treat it as not installed).
    """
    for name in _CONFIG_FILENAMES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    return None
