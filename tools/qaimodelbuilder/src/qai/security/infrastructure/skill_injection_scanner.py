# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Prompt-injection scanner for SKILL.md bodies (PR-504).

Aligns with the legacy
``backend/security/skill_injection_scanner.py:181`` public surface:

* :class:`ThreatPattern` — value object: ``(name, pattern, severity,
  description)``.
* :class:`Threat` — scan result row: ``(pattern_name, severity,
  description, matched_text)``.
* :func:`scan` — accept an arbitrary skill-body string, return
  ``tuple[Threat, ...]`` of detected threats (empty tuple = safe).

PR-504 places this in ``qai.security.infrastructure`` because it is
sync, not part of the application port surface, and uses regular
expressions (which strict adapters would treat as IO-equivalent).
The :class:`RegisterSkillCapabilityUseCase` (in
``qai.security.application.use_cases``) consumes the scanner output
when a skill is being registered: any high-severity threat blocks
the registration with :class:`SkillCapabilityViolation`.

Threat patterns mirror the 6 patterns in the legacy module:

1. ``prompt_injection`` — ``ignore (previous|all|above|prior)
   instructions``.
2. ``deception_hide`` — ``do not tell the user``.
3. ``sys_prompt_override`` — ``system prompt override``.
4. ``disregard_rules`` — ``disregard (your|all|any) (instructions|
   rules|guidelines)``.
5. ``html_comment_injection`` — ``<!-- ... ignore|override|system|
   secret|hidden ... -->``.
6. ``exfil_curl`` — ``curl ... ${API_KEY|SECRET|TOKEN|PASSWORD|
   OPENAI_API_KEY|AWS_SECRET}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Threat",
    "ThreatPattern",
    "BUILTIN_THREAT_PATTERNS",
    "scan",
    "scan_text",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ThreatPattern:
    """A single regex-driven detection rule."""

    name: str
    regex: re.Pattern[str]
    severity: str  # "high" | "medium" | "low"
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Threat:
    """A detected threat — one match of a :class:`ThreatPattern`."""

    pattern_name: str
    severity: str
    description: str
    matched_text: str


BUILTIN_THREAT_PATTERNS: tuple[ThreatPattern, ...] = (
    ThreatPattern(
        name="prompt_injection",
        regex=re.compile(
            r"ignore\s+(previous|all|above|prior)\s+instructions",
            re.IGNORECASE,
        ),
        severity="high",
        description=(
            "Attempts to override prior instructions via "
            "'ignore ... instructions' pattern"
        ),
    ),
    ThreatPattern(
        name="deception_hide",
        regex=re.compile(
            r"do\s+not\s+tell\s+the\s+user",
            re.IGNORECASE,
        ),
        severity="high",
        description="Attempts to hide information from the user",
    ),
    ThreatPattern(
        name="sys_prompt_override",
        regex=re.compile(
            r"system\s+prompt\s+override",
            re.IGNORECASE,
        ),
        severity="high",
        description="Attempts to override the system prompt",
    ),
    ThreatPattern(
        name="disregard_rules",
        regex=re.compile(
            r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)",
            re.IGNORECASE,
        ),
        severity="high",
        description=(
            "Attempts to make the model disregard its instructions"
            " or rules"
        ),
    ),
    ThreatPattern(
        name="html_comment_injection",
        regex=re.compile(
            r"<!--[^>]*(ignore|override|system|secret|hidden)[^>]*-->",
            re.IGNORECASE,
        ),
        severity="medium",
        description=(
            "HTML comments containing suspicious keywords "
            "(ignore/override/system/secret/hidden)"
        ),
    ),
    ThreatPattern(
        name="exfil_curl",
        regex=re.compile(
            r"curl\s+.*\$\{?\s*(API_KEY|SECRET|TOKEN|PASSWORD"
            r"|OPENAI_API_KEY|AWS_SECRET)",
            re.IGNORECASE,
        ),
        severity="high",
        description=(
            "Curl command referencing sensitive environment "
            "variables (potential data exfiltration)"
        ),
    ),
)


def scan(
    body: str,
    *,
    patterns: tuple[ThreatPattern, ...] = BUILTIN_THREAT_PATTERNS,
) -> tuple[Threat, ...]:
    """Scan ``body`` against ``patterns`` and return detected threats.

    Returns the empty tuple when no pattern matches. Multiple matches
    of the same pattern are reported once (the first match) so the
    output stays bounded; callers needing every occurrence should
    iterate over ``pattern.regex.finditer`` themselves.
    """
    if not isinstance(body, str):
        raise TypeError(
            f"body must be str, got {type(body).__name__}"
        )
    threats: list[Threat] = []
    for tp in patterns:
        match = tp.regex.search(body)
        if match is None:
            continue
        threats.append(
            Threat(
                pattern_name=tp.name,
                severity=tp.severity,
                description=tp.description,
                matched_text=match.group(0),
            )
        )
    return tuple(threats)


def scan_text(text: str) -> tuple[Threat, ...]:
    """Convenience alias of :func:`scan`."""
    return scan(text)
