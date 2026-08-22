# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.platform.model_weights``.

Shared kernel for NPU weight discovery and precision normalisation. Both
``qai.model_builder`` (Pack export) and ``qai.app_builder`` (import wizard)
depend on this module so the two can never disagree about which files are
weights or what a precision token means.

Core rules:

1. **discover by file extension, attach precision afterwards** — an absent
   precision suffix never drops the file;
2. **an unrecognised precision keeps its own identity** — a declared
   ``mixed_with_float`` stays ``mixed_with_float`` rather than being
   relabelled ``fp16``, because not dropping a file is no excuse for
   describing it wrongly;
3. **a precision is a set of files, not one file each** — see
   :func:`partition_component_weights`.
"""

from __future__ import annotations

from .diagnostics import WeightSearchReport, build_search_report
from .discovery import (
    MIN_WEIGHT_BYTES,
    WeightCandidate,
    discover_weights,
    partition_component_weights,
    select_best_per_precision,
)
from .precision import (
    DEFAULT_PRECISION,
    KNOWN_PRECISION_LABELS,
    WEIGHT_SUFFIXES,
    display_label,
    is_known_precision_token,
    label_for,
    normalise_precision,
    precision_from_filename,
)

__all__ = [
    "DEFAULT_PRECISION",
    "KNOWN_PRECISION_LABELS",
    "MIN_WEIGHT_BYTES",
    "WEIGHT_SUFFIXES",
    "WeightCandidate",
    "WeightSearchReport",
    "build_search_report",
    "discover_weights",
    "display_label",
    "is_known_precision_token",
    "label_for",
    "normalise_precision",
    "partition_component_weights",
    "precision_from_filename",
    "select_best_per_precision",
]
