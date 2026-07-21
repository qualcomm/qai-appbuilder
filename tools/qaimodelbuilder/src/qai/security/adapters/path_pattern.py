# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Normalised :class:`PathPattern` matcher (PR-092 §2.1 C-1 / §17.5 #11).

The domain-layer :class:`qai.security.domain.value_objects.PathPattern`
performs raw ``fnmatch`` glob matching, intentionally pure (no OS
calls so it stays import-linter ``domain-purity``-clean). Real policy
evaluation needs to compare *normalised* paths — 8.3 short names,
symlinks, mixed slashes — against *normalised* patterns so a rule
like ``C:\\Program Files\\*`` still matches a candidate that arrived
as ``C:\\PROGRA~1\\foo``.

This adapter pairs the pure VO with
:func:`qai.security.adapters.path_normalizer.normalize_path` so the
audit hook, sandbox grant repository and decision cache all see the
same canonical form.
"""

from __future__ import annotations

from qai.security.adapters.path_normalizer import normalize_path
from qai.security.domain.value_objects import PathPattern

__all__ = ["normalised_match"]


def normalised_match(pattern: PathPattern, candidate: "str | None") -> bool:
    """Return ``True`` iff ``candidate`` matches ``pattern`` after normalisation.

    Both the pattern source and the candidate path are passed through
    :func:`normalize_path` before delegating to
    :meth:`PathPattern.matches`. Empty / blank candidates produce
    ``False`` (mirrors the pure VO behaviour).
    """

    if not candidate:
        return False
    norm_candidate = str(normalize_path(candidate))
    if not norm_candidate:
        return False
    norm_pattern_str = str(normalize_path(pattern.pattern))
    if not norm_pattern_str:
        # Fall back to the raw pattern when normalisation strips it
        # (e.g. blank input) so the original VO match still fires.
        norm_pattern_str = pattern.pattern
    return PathPattern(
        pattern=norm_pattern_str,
        case_sensitive=pattern.case_sensitive,
    ).matches(norm_candidate)
