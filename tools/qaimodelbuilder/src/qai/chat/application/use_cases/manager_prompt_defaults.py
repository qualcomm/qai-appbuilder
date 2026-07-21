# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Manager (moderator) prompt customization — central constants + resolver.

DISC-2 P4-step2 (§22A.7, the final step): the Manager selector mode gains an
optional, user-supplied **scheduling-preference** segment appended to the
moderator system prompt.  The design is *default manager prompt + user append*:

* the immutable protocol segment stays FIRST and verbatim (the MODERATOR role
  definition, the "reply with EXACTLY one participant id" instruction, the END
  sentinel block when early-end is enabled, and the roster list);
* the user's append text can ONLY add *scheduling preferences* (advisory) and is
  injected at the **end** of the system prompt, so it can never override or
  shadow the protocol segment that precedes it.

This is append-only, never override: even if a user writes something that
contradicts the protocol, the Manager output is still parsed against the roster
and any illegal / unparseable reply degrades deterministically to round-robin
(``ManagerAgentSelector.select_next`` — State-Truth-First), so the append can
never break speaker selection or the END gate.

Two ``meta["discussion"]`` keys (§3.1 tail-append):

* ``manager_prompt_customization_mode`` — one of
  :data:`MANAGER_PROMPT_CUSTOMIZATION_MODES`:

  - ``none`` (the DEFAULT) — no append; the moderator prompt is byte-for-byte
    the phase-1 (P1-step3) prompt;
  - ``append_instruction`` (the MVP-open mode) — append the user's
    ``manager_prompt_append`` text as an advisory scheduling preference;
  - ``advanced_override`` — RESERVED, NOT open in the MVP; coerced to ignore the
    append (resolves to ``None``) so a future full-override mode can land without
    a contract change.

* ``manager_prompt_append`` — the user's scheduling-preference text (truncated to
  :data:`MANAGER_PROMPT_APPEND_MAX_CHARS` so it cannot crowd out the manager wire
  budget).

**Read-side semantics (keeps every existing manager test byte-for-byte):** a
missing / ``none`` mode, or an empty append, resolves to ``None`` (no append) —
i.e. the phase-1 behaviour.

**Front-end simplification convention:** the front-end exposes only a single
``manager_prompt_append`` textarea and does NOT send an explicit mode.  When the
mode is missing / ``None`` but the append text is non-empty, the resolver treats
it as ``append_instruction`` (append non-empty ⇒ append).  An explicit ``none``
or ``advanced_override`` mode always wins and ignores the append.  This keeps the
front-end UI minimal while the backend contract still carries the full mode field
for a future advanced UI.

Layering: ``application/use_cases`` — pure constants + a pure function over
stdlib only.  No ports / domain / adapters, so the layering contracts hold.
"""

from __future__ import annotations

__all__ = [
    "MANAGER_PROMPT_CUSTOMIZATION_MODE_KEY",
    "MANAGER_PROMPT_APPEND_KEY",
    "MANAGER_PROMPT_CUSTOMIZATION_MODES",
    "DEFAULT_MANAGER_PROMPT_CUSTOMIZATION_MODE",
    "MANAGER_PROMPT_APPEND_MAX_CHARS",
    "resolve_manager_prompt_append",
]


# ── Key names (wire snake_case == meta["discussion"] keys) ───────────────────
MANAGER_PROMPT_CUSTOMIZATION_MODE_KEY = "manager_prompt_customization_mode"
MANAGER_PROMPT_APPEND_KEY = "manager_prompt_append"


# ── Modes / defaults ─────────────────────────────────────────────────────────
#: The closed set of legal customization modes.
#:
#: * ``none`` — default, no append;
#: * ``append_instruction`` — MVP-open, append the user's text as advisory;
#: * ``advanced_override`` — RESERVED, NOT open in the MVP (coerced to ignore the
#:   append).  Kept in the closed set so the contract is stable for a future
#:   full-override mode.
MANAGER_PROMPT_CUSTOMIZATION_MODES: tuple[str, ...] = (
    "none",
    "append_instruction",
    "advanced_override",
)

#: The read-side default AND the new-conversation default.  Resolving a missing
#: mode (with an empty append) to this keeps the phase-1 manager prompt intact.
DEFAULT_MANAGER_PROMPT_CUSTOMIZATION_MODE = "none"

#: Hard cap on the appended text (chars).  Bounds how much of the manager wire
#: budget the user append may consume so the protocol segment + roster always fit.
MANAGER_PROMPT_APPEND_MAX_CHARS = 2000


def resolve_manager_prompt_append(discussion: dict | None) -> str | None:
    """Resolve the EFFECTIVE manager-prompt append text from ``meta["discussion"]``.

    Returns the append text (stripped + truncated to
    :data:`MANAGER_PROMPT_APPEND_MAX_CHARS`) ONLY when it should be applied;
    otherwise ``None`` (no append → phase-1 manager prompt unchanged).

    Decision table (mode → effect):

    * ``append_instruction`` + non-empty append → return the append text;
    * missing / ``None`` mode + non-empty append → treated as
      ``append_instruction`` (the front-end simplification convention: a
      non-empty append implies the open mode) → return the append text;
    * ``none`` (explicit) → ``None`` (ignore any append);
    * ``advanced_override`` → ``None`` (RESERVED, NOT open in the MVP — coerced
      to ignore the append; a future full-override mode lands here);
    * empty / whitespace-only append (any mode) → ``None``;
    * unknown / illegal mode → ``None`` (no append, safe fallback).
    """
    d = discussion or {}
    raw_append = d.get(MANAGER_PROMPT_APPEND_KEY)
    append = raw_append.strip() if isinstance(raw_append, str) else ""
    if not append:
        return None

    raw_mode = d.get(MANAGER_PROMPT_CUSTOMIZATION_MODE_KEY)
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""

    # Front-end simplification: a missing/blank mode with a non-empty append
    # implies the open ``append_instruction`` mode (the UI only exposes the
    # textarea, not a mode picker).
    if mode in ("", "append_instruction"):
        return append[:MANAGER_PROMPT_APPEND_MAX_CHARS]

    # ``none`` (explicit) and ``advanced_override`` (RESERVED, not open) both
    # ignore the append; any unknown mode is treated as no-append (safe).
    return None
