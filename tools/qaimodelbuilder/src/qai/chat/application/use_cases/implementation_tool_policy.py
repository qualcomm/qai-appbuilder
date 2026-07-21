# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation-mode tool policy — pure intersection helpers (DISC-1 §22.5).

DISC-1 step2 "safe ground floor": the multi-layer tool-permission **intersection**
that an implementation-mode speaker turn will eventually be gated by, expressed as
side-effect-free PURE FUNCTIONS plus a static tool→level classification table.

🔴 ZERO call sites in step2.  Nothing in this module is invoked from any execution
path — :func:`compute_effective_implementation_tools` computes the "effective tool
set" but DOES NOT grant any actual tool.  The real consumption (gating
``_run_speaker_turn`` / ``_speaker_tool_schemas`` for an implementation turn) lands
in step3, AFTER the user picks the tool tier (§22.12#2).  Until then this is purely
the越权-interception math + conservative defaults, unit-tested in isolation so the
"a role never gains a tool it does not own" and "dangerous tools are denied by
default" guarantees are proven BEFORE any tool is unlocked (§22.3a).

Real tool names (NOT the design doc's placeholders) — sourced from the live tool
registry:

* **safe** — ``read`` / ``list`` / ``grep`` / ``glob`` (read-only inspection).
* **write** — ``write`` / ``edit`` / ``apply_patch`` (filesystem mutation).
* **exec_limited** — ``exec`` (sandboxed command execution).
* **dangerous** — ``webfetch`` (network egress) etc.; DENIED by default.  There is
  no standalone delete tool; genuinely destructive operations ride ``exec`` and are
  contained by the ai_coding sandbox denylist at runtime, so ``dangerous`` here is a
  conceptual placeholder that stays OFF until explicitly opted in.

Layering: ``application/use_cases`` — depends on stdlib only.  No ports, no domain,
no adapters, so ``layered-chat`` / ``context-isolation`` hold and the helpers are
reusable from tests directly (mirrors :mod:`discussion_mode` /
:mod:`convergence_defaults`).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "SAFE_TOOLS",
    "WRITE_TOOLS",
    "EXEC_LIMITED_TOOLS",
    "DANGEROUS_TOOLS",
    "TOOL_LEVELS",
    "ToolAccessLevel",
    "LEVEL_TO_TOOLS",
    "IMPLEMENTATION_MODE_DEFAULT_LEVELS",
    "implementation_mode_allowlist",
    "compute_effective_implementation_tools",
]


# ---------------------------------------------------------------------------
# Tool→level classification table (real tool names)
# ---------------------------------------------------------------------------
#: Read-only inspection tools — always the safest tier.
SAFE_TOOLS: Final[frozenset[str]] = frozenset({"read", "list", "grep", "glob"})
#: Filesystem-mutating tools.
WRITE_TOOLS: Final[frozenset[str]] = frozenset({"write", "edit", "apply_patch"})
#: Sandboxed command execution.
EXEC_LIMITED_TOOLS: Final[frozenset[str]] = frozenset({"exec"})
#: Network/egress and other genuinely dangerous capabilities — DENIED by default.
DANGEROUS_TOOLS: Final[frozenset[str]] = frozenset({"webfetch"})


class ToolAccessLevel:
    """The four implementation-mode tool tiers (string constants, §22.5).

    Plain string constants (not an ``Enum``) so they slot straight into the
    ``frozenset[str]`` level sets the helpers below operate on, and so callers
    never need an extra ``.value`` unwrap.
    """

    SAFE: Final[str] = "safe"
    WRITE: Final[str] = "write"
    EXEC_LIMITED: Final[str] = "exec_limited"
    DANGEROUS: Final[str] = "dangerous"


#: The four tier names as a frozenset (for validation / iteration).
TOOL_LEVELS: Final[frozenset[str]] = frozenset(
    {
        ToolAccessLevel.SAFE,
        ToolAccessLevel.WRITE,
        ToolAccessLevel.EXEC_LIMITED,
        ToolAccessLevel.DANGEROUS,
    }
)

#: Maps each tier name to its tool-name set — the single source of truth used to
#: expand a set of allowed LEVELS into a set of allowed TOOL NAMES.
LEVEL_TO_TOOLS: Final[dict[str, frozenset[str]]] = {
    ToolAccessLevel.SAFE: SAFE_TOOLS,
    ToolAccessLevel.WRITE: WRITE_TOOLS,
    ToolAccessLevel.EXEC_LIMITED: EXEC_LIMITED_TOOLS,
    ToolAccessLevel.DANGEROUS: DANGEROUS_TOOLS,
}

#: The tiers an implementation-mode turn opens by DEFAULT (DISC-1 §22.5 一期):
#: ``safe`` + ``write`` + ``exec_limited``.  ``dangerous`` is deliberately ABSENT
#: — network/destructive capabilities require an explicit opt-in (§22.5:1324-1329).
IMPLEMENTATION_MODE_DEFAULT_LEVELS: Final[frozenset[str]] = frozenset(
    {
        ToolAccessLevel.SAFE,
        ToolAccessLevel.WRITE,
        ToolAccessLevel.EXEC_LIMITED,
    }
)


def implementation_mode_allowlist(
    levels: frozenset[str] = IMPLEMENTATION_MODE_DEFAULT_LEVELS,
) -> frozenset[str]:
    """Expand a set of allowed tiers into the union of their tool names.

    Pure + deterministic.  Unknown tier names contribute nothing (they map to an
    empty set) so a typo can only ever *narrow* the allowlist, never widen it.
    ``dangerous`` tools appear ONLY when the caller explicitly includes the
    ``dangerous`` tier (it is excluded from
    :data:`IMPLEMENTATION_MODE_DEFAULT_LEVELS`).
    """
    out: set[str] = set()
    for level in levels:
        out |= LEVEL_TO_TOOLS.get(level, frozenset())
    return frozenset(out)


def compute_effective_implementation_tools(
    *,
    role_tools: set[str],
    mode_allowlist: frozenset[str] | None = None,
    workspace_allowlist: frozenset[str] | None = None,
    session_approved: frozenset[str] | None = None,
) -> frozenset[str]:
    """Compute the effective implementation-mode tool set (DISC-1 §22.5).

    🔴 step2 has NO call site for this function — it computes the intersection an
    implementation turn WOULD be gated by, but grants nothing.  step3 wires it in
    front of the speaker turn after the tool tier is chosen (§22.12#2).

    The effective set is the intersection of up to four layers::

        role_tools ∩ mode_allowlist ∩ workspace_allowlist ∩ session_approved

    * ``role_tools`` — the tools the speaking role actually owns (its
      ``allowed_tools``).  This is the floor: a role can NEVER gain a tool it does
      not own, no matter how permissive the other layers are (intersection
      semantics — proven by ``test_role_without_tool_does_not_gain_it``).
    * ``mode_allowlist`` — the tools the implementation mode opens; defaults to
      :func:`implementation_mode_allowlist` over
      :data:`IMPLEMENTATION_MODE_DEFAULT_LEVELS` (so ``dangerous`` tools are
      denied unless the mode is explicitly widened).
    * ``workspace_allowlist`` — a per-workspace narrowing (step-later layer).
    * ``session_approved`` — tools the user approved this session (step-later
      layer).

    ``None`` is the IDENTITY for the workspace / session layers (one-期: those
    guardrails are enforced at runtime by the ai_coding sandbox, so passing
    ``None`` here applies no narrowing — it does NOT widen the set).  Mirrors the
    pure intersection + ``None``-identity style of
    :func:`discussion_mode.advertised_tool_names`.
    """
    effective = frozenset(role_tools)

    if mode_allowlist is None:
        mode_allowlist = implementation_mode_allowlist()
    effective &= mode_allowlist

    if workspace_allowlist is not None:
        effective &= workspace_allowlist

    if session_approved is not None:
        effective &= session_approved

    return effective
