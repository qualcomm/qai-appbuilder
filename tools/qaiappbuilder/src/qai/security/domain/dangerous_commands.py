# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Dangerous-command deny floor (security domain, always-on built-in).

The pure-software exec safety layer (``PatternFileScreen`` / the FileBroker
bridge) hard-blocks a small, high-confidence set of destructive commands
(``rm -rf`` / ``del /s`` / ``format C:`` / fork-bomb / raw-disk write …) so
basic hygiene works even while the heavier FileGuard PolicyCenter is OFF.
These were V1 ``exec_deny`` defaults; V2 previously kept them as a
hard-coded ``DANGEROUS_COMMAND_PATTERNS`` constant in the apps-layer
FileBroker bridge with **no** runtime override at all.

Phase 3a (config-source unification, P-18 §6.2) promotes them here as the
security domain's **immutable built-in floor** and adds a *union-only*
runtime-override layer:

* :data:`BUILTIN_DANGEROUS_COMMAND_PATTERNS` — the non-removable floor. An
  operator can NEVER delete these (red line §9.2.4: no "one-click disable
  ``rm -rf`` protection").
* :func:`dangerous_command_patterns` — returns the floor UNIONed with any
  operator-supplied extra patterns. Extra patterns can only ADD coverage;
  the floor is always present.
* :func:`match_dangerous_command` — return the first matching pattern's
  source (for the deny reason) or ``None``.

Pure domain: standard library only, no framework / cross-context imports.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "BUILTIN_DANGEROUS_COMMAND_PATTERNS",
    "compile_extra_patterns",
    "dangerous_command_patterns",
    "match_dangerous_command",
]


#: Immutable built-in dangerous-command floor (V1 ``exec_deny`` default-set
#: parity). Intentionally a small, high-confidence set of destructive
#: commands — no DB / PolicyCenter dependency, so it works while FileGuard is
#: OFF. This floor is NON-REMOVABLE: the runtime-override layer can only
#: UNION additional patterns on top (never delete these). See red line
#: §9.2.4 (no one-click disable of the ``rm -rf`` floor).
BUILTIN_DANGEROUS_COMMAND_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # rm with a combined recursive+force flag in EITHER order (-rf / -fr /
    # -Rf / -frv …). Lookaheads require the flag token to contain both an
    # ``r`` and an ``f``; a lone -r or -f (or ``rm file``) is NOT matched.
    # (P-22 fix — the old ``-[a-z]*r[a-z]*f`` required r-before-f and missed
    # ``rm -fr``.)
    re.compile(r"\brm\s+-(?=[a-z]*r)(?=[a-z]*f)[a-z]+", re.IGNORECASE),
    re.compile(r"\brmdir\s+/s", re.IGNORECASE),            # rmdir /s
    re.compile(r"\bdel\s+/[a-z]*s", re.IGNORECASE),        # del /s
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),       # format C:
    re.compile(r"\bmkfs\b", re.IGNORECASE),                # mkfs
    re.compile(r"\bdd\s+if=", re.IGNORECASE),              # dd if=
    re.compile(r":\s*\(\s*\)\s*\{.*\|\s*:", re.DOTALL),    # fork bomb :(){ :|:
    # Remove-Item carrying BOTH -Recurse and -Force in EITHER order. Uses
    # lookaheads with ``(?<![\w-])`` flag boundaries (a space→``-`` position
    # has no ``\b``, so the old ``\b-Recurse\b`` form NEVER fired — P-22 fix).
    re.compile(
        r"\bRemove-Item\b"
        r"(?=.*(?<![\w-])-Recurse\b)"
        r"(?=.*(?<![\w-])-Force\b)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),        # write to raw disk
)


def compile_extra_patterns(
    patterns: "tuple[str, ...] | list[str] | None",
) -> tuple[re.Pattern[str], ...]:
    """Compile operator-supplied extra dangerous-command regex strings.

    Invalid / uncompilable patterns are skipped (best-effort — a bad operator
    entry must never crash the guard or, worse, disable the floor). Returns an
    empty tuple for ``None`` / empty input.
    """
    if not patterns:
        return ()
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error:
            # Skip a malformed operator pattern; the built-in floor still
            # applies. (Never raise: a bad extra pattern cannot open the box.)
            continue
    return tuple(compiled)


def dangerous_command_patterns(
    extra: "tuple[re.Pattern[str], ...] | None" = None,
) -> tuple[re.Pattern[str], ...]:
    """Return the built-in floor UNIONed with ``extra`` patterns.

    The floor (:data:`BUILTIN_DANGEROUS_COMMAND_PATTERNS`) is ALWAYS present
    and comes first; ``extra`` (already-compiled) patterns are appended. This
    is union-only by construction — there is no code path that removes a
    built-in pattern (red line §9.2.4).
    """
    if not extra:
        return BUILTIN_DANGEROUS_COMMAND_PATTERNS
    return BUILTIN_DANGEROUS_COMMAND_PATTERNS + tuple(extra)


def match_dangerous_command(
    command: str,
    *,
    extra: "tuple[re.Pattern[str], ...] | None" = None,
) -> str | None:
    """Return the first matching pattern's source string, or ``None``.

    Checks ``command`` against the built-in floor plus any ``extra`` patterns
    (union). The returned pattern source is used by the caller to build the
    deny reason.
    """
    for pattern in dangerous_command_patterns(extra):
        if pattern.search(command):
            return pattern.pattern
    return None
