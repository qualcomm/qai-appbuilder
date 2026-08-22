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

from functools import lru_cache

from qai.security.adapters.path_normalizer import normalize_path
from qai.security.domain.value_objects import PathPattern

__all__ = ["normalised_match", "clear_normalise_cache"]


# L-Sec-1 (fileguard-audit-2026-07-26 M/L §Security): ``normalize_path``
# resolves symlinks + strips 8.3 short names on every call. In the hot
# path (audit hook + decision cache + grant repo all match each candidate
# against a policy's pattern list) the SAME candidate string is normalised
# repeatedly. A bounded LRU cache keyed on the raw input string collapses
# that to one filesystem probe per unique input; size=512 covers a typical
# ASK burst (a subprocess touching ~dozens of paths) with headroom, and
# ``lru_cache`` gives O(1) amortised hit-cost. Cache is INPUT-KEYED (raw
# string in, normalised string out); if the underlying filesystem topology
# changes at runtime callers can invoke :func:`clear_normalise_cache` — the
# policy-reload hook already recomputes derived state so it's a natural
# place to drop stale entries.
@lru_cache(maxsize=512)
def _normalize_cached(raw: str) -> str:
    return str(normalize_path(raw))


def clear_normalise_cache() -> None:
    """Drop every cached normalised path.

    Called by the PolicyReloaded event handler / test teardown when the
    normalised view of a path may have changed under our feet (e.g. a
    symlink was rewritten). Safe to call at any time.
    """
    _normalize_cached.cache_clear()


def normalised_match(pattern: PathPattern, candidate: "str | None") -> bool:
    """Return ``True`` iff ``candidate`` matches ``pattern`` after normalisation.

    Both the pattern source and the candidate path are passed through
    :func:`normalize_path` (via :func:`_normalize_cached`) before delegating
    to :meth:`PathPattern.matches`. Empty / blank candidates produce
    ``False`` (mirrors the pure VO behaviour).
    """

    if not candidate:
        return False
    norm_candidate = _normalize_cached(candidate)
    if not norm_candidate:
        return False
    norm_pattern_str = _normalize_cached(pattern.pattern)
    if not norm_pattern_str:
        # Fall back to the raw pattern when normalisation strips it
        # (e.g. blank input) so the original VO match still fires.
        norm_pattern_str = pattern.pattern
    return PathPattern(
        pattern=norm_pattern_str,
        case_sensitive=pattern.case_sensitive,
    ).matches(norm_candidate)
