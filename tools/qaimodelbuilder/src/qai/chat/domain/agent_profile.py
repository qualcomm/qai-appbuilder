# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sub-agent *profiles* for the chat bounded context.

A :class:`AgentProfile` is a pure value object that captures the behavioural
shape of a sub-agent the main agent can spawn via the ``agent`` tool:

* its ``name`` — the persisted :attr:`SubAgentSession.subagent_type` value
  (e.g. ``"general"`` / ``"explore"``);
* an optional ``system_prompt`` override (``None`` keeps the handler's default
  focused sub-agent prompt — used by ``general`` for full backward
  compatibility);
* a tool *allow*/*deny* policy (``allowed_tools`` / ``denied_tools``) applied
  ON TOP OF the existing sub-agent tool filtering, letting a profile restrict
  the sub-agent to a narrower, safer tool set.

Two built-in profiles ship today:

* :data:`GENERAL` — the historical sub-agent behaviour (no extra denials, no
  prompt override). Choosing it (or omitting ``subagent_type`` entirely) is a
  byte-for-byte no-op versus before this module existed.
* :data:`EXPLORE` — a STRICTLY READ-ONLY codebase-search specialist: it may
  only read / search (``read`` / ``glob`` / ``grep`` / ``webfetch`` /
  ``list`` / ``skill``) and is denied every state-mutating tool (write / edit /
  apply_patch / exec / app-builder runners / todowrite / background_process)
  plus the tools the sub-agent never receives anyway (``agent`` / ``question`` /
  ``list_subagents``).

This is a DOMAIN value object: it depends only on the standard library, has no
side effects, and is consumed by the adapters/application layers (the
``agent`` tool handler resolves a profile from the model-supplied
``subagent_type`` and applies it). The domain never imports fastapi /
sqlalchemy / pydantic_settings / apps / interfaces, so the ``domain-purity``
and ``layered-chat`` import-linter contracts stay intact.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentProfile:
    """Immutable description of a sub-agent behavioural profile.

    Attributes
    ----------
    name:
        Stable identifier persisted as
        :attr:`~qai.chat.domain.sub_agent_session.SubAgentSession.subagent_type`.
    description:
        Short human-readable summary (surfaced in the ``agent`` tool schema's
        ``subagent_type`` enum description so the model can self-select).
    system_prompt:
        Optional system-prompt override. ``None`` means "use the handler's
        default focused sub-agent prompt" (the ``general`` behaviour).
    allowed_tools:
        When not ``None``, an ALLOW-LIST: only tools whose name is in this set
        survive profile filtering (applied after the base sub-agent exclusions).
        ``None`` means "no allow-list — every tool that passed the base filter
        is kept" (the ``general`` behaviour).
    denied_tools:
        A DENY-LIST always subtracted from the advertised tools (applied
        whether or not an allow-list is present). Empty for ``general``.
    model:
        Optional per-profile model id (a model-hint string). ``None`` (or a
        blank string) means "inherit the parent turn's ``model_hint``" — the
        historical behaviour. When set, the sub-agent spawned for this profile
        routes to THIS model instead of the main agent's. This field is a pure
        VALUE carried by the profile; the built-ins leave it ``None`` and the
        adapters layer fills it (via
        :func:`resolve_profile`'s ``model_override``) from the user's
        per-profile configuration — the domain hard-codes NO model id and NO
        tier table.
    max_rounds:
        Optional static per-profile round budget the user need not configure.
        ``None`` means "use the dispatcher's shared ``_max_rounds``" (the
        historical behaviour — :data:`GENERAL` keeps it ``None``).
        :data:`EXPLORE` sets ``5`` because a read-only search specialist should
        converge in a few focused search rounds rather than burn the full
        general allowance.
    """

    name: str
    description: str
    system_prompt: str | None = None
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    model: str | None = None
    max_rounds: int | None = None

    def filter_tool_names(self, names: frozenset[str]) -> frozenset[str]:
        """Apply this profile's allow/deny policy to *names*.

        ``names`` is the set of tool names that already survived the base
        sub-agent filtering (``_SUB_AGENT_EXCLUDED_TOOLS`` + conditional
        app-builder handling). Returns the subset this profile permits:

        * drop everything in :attr:`denied_tools`;
        * when :attr:`allowed_tools` is set, keep ONLY names also in it.

        Pure — returns a new frozenset, mutates nothing. For :data:`GENERAL`
        (empty deny-list, no allow-list) this returns ``names`` unchanged, so
        the sub-agent tool set is identical to the pre-profile behaviour.
        """
        kept = names - self.denied_tools
        if self.allowed_tools is not None:
            kept = kept & self.allowed_tools
        return kept


# Tools the explore profile must NEVER receive on top of the base sub-agent
# exclusions (``agent`` / ``question`` / ``list_subagents`` are already dropped
# by ``_SUB_AGENT_EXCLUDED_TOOLS``; they are re-listed here so the deny-list is
# self-contained and explicit about explore's read-only contract).
_EXPLORE_DENIED_TOOLS: frozenset[str] = frozenset(
    {
        # State-mutating tools — strictly forbidden for a read-only explorer.
        "write",
        "edit",
        "apply_patch",
        "exec",
        "appbuilder_run",
        "appbuilder_batch_run",
        "todowrite",
        "background_process",
        # Sub-agent base exclusions (re-stated for an explicit contract).
        "agent",
        "question",
        "list_subagents",
    }
)

# The ONLY tools an explore sub-agent may use — a read-only search surface.
# ``skill`` is intentionally NOT here: autonomous sub-agents (explore included)
# get no skill by product decision (方案Y). The compose layer also drops skill
# via ``enable_skill=False``, so this whitelist and that gate agree.
_EXPLORE_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "glob",
        "grep",
        "webfetch",
        "list",
    }
)

# Explore profile system prompt (English; adapted for this project — a strictly
# read-only file-search specialist). Mirrors the intent of a dedicated
# "explore" agent: navigate/search the codebase fast, never mutate state.
_EXPLORE_SYSTEM_PROMPT = (
    "You are a file search specialist. You excel at thoroughly navigating and "
    "exploring codebases to locate relevant files, symbols, and code.\n\n"
    "Your strengths:\n"
    "- Rapidly finding files using glob patterns (`glob`).\n"
    "- Searching code and text with powerful regex patterns (`grep`).\n"
    "- Reading and analysing file contents (`read`).\n"
    "- Fetching reference material from the web when needed (`webfetch`).\n\n"
    "Guidelines:\n"
    "- Use `glob` for broad file pattern matching, `grep` for content search "
    "with regex, and `read` when you already know the specific file path.\n"
    "- Adapt the depth/breadth of your search to the thoroughness level the "
    "caller asked for: a quick lookup needs one or two targeted searches; a "
    "thorough audit warrants iterating across many patterns and files.\n"
    "- Report findings as ABSOLUTE file paths with line numbers (e.g. "
    "`C:\\path\\to\\file.py:123`) so the caller can navigate directly.\n"
    "- Be concise. The full output of each tool call is already shown to the "
    "user in a collapsible panel — do NOT paste raw file contents or search "
    "dumps back into your text; summarise what you found and where.\n\n"
    "STRICTLY READ-ONLY: you MUST NOT modify any file or change system state "
    "in any way. You have NO write / edit / apply_patch / exec tools — never "
    "ask for them or attempt a workaround. Your job is to find and report, "
    "not to change anything.\n\n"
    "FILESYSTEM SAFETY: avoid recursive scans rooted at an UNBOUNDED, large "
    "directory — a drive root (`C:\\`), a broad shared/user tree, or an entire "
    "unrelated repository. Such a scan traverses hundreds of thousands of "
    "files and can hang for minutes. Recursing inside your own bounded "
    "workspace or a known sub-directory is fine; the `glob`/`grep` tools cap "
    "results and skip heavyweight directories. When you need a file outside "
    "your workspace, prefer the exact path the prompt gives you over a broad "
    "search."
)


# ── Built-in profiles ──────────────────────────────────────────────────────
# GENERAL is the historical sub-agent: no prompt override, no extra denials, no
# allow-list — so resolving it (or omitting ``subagent_type``) is a no-op versus
# the pre-profile behaviour (regression protection).
GENERAL = AgentProfile(
    name="general",
    description=(
        "General-purpose sub-agent: full tool set (read/write/edit/exec/"
        "glob/grep/webfetch/…), runs a multi-step agentic loop until the "
        "delegated task is complete. Use for tasks that may need to CHANGE "
        "files or run commands."
    ),
    system_prompt=None,
    allowed_tools=None,
    denied_tools=frozenset(),
)

# EXPLORE is a strictly read-only codebase-search specialist.
EXPLORE = AgentProfile(
    name="explore",
    description=(
        "Read-only codebase exploration sub-agent: ONLY read/glob/grep/"
        "webfetch (no write/edit/exec). Use for fast, safe code-base search "
        "and investigation when you only need to FIND and READ, never change."
    ),
    system_prompt=_EXPLORE_SYSTEM_PROMPT,
    allowed_tools=_EXPLORE_ALLOWED_TOOLS,
    denied_tools=_EXPLORE_DENIED_TOOLS,
    # A read-only search specialist should converge in a few focused search
    # rounds; cap its loop at 5 (vs GENERAL's inherited shared budget) so an
    # explore sub-agent never spins the full general allowance on a lookup.
    max_rounds=5,
)

# Registry of built-in profiles keyed by name. GENERAL is the default.
_PROFILES: dict[str, AgentProfile] = {
    GENERAL.name: GENERAL,
    EXPLORE.name: EXPLORE,
}

# The persisted ``subagent_type`` of the original (pre-profile) sub-agent was
# the literal ``"agent"``. Treat it as GENERAL so already-stored sessions and
# any caller passing the legacy value resolve to the historical behaviour.
_LEGACY_GENERAL_ALIAS = "agent"


def resolve_profile(
    name: str | None,
    *,
    model_override: str | None = None,
) -> AgentProfile:
    """Resolve a profile name to its :class:`AgentProfile`.

    ``None``, an empty/unknown name, or the legacy ``"agent"`` alias all
    resolve to :data:`GENERAL` (the historical behaviour) — a sub-agent is
    NEVER blocked from running by an unrecognised ``subagent_type``; it simply
    falls back to the general profile. Matching is case-insensitive on a
    trimmed name.

    ``model_override`` (kw-only, V2 enhancement — per-profile model) overrides
    the resolved profile's :attr:`AgentProfile.model`:

    * a NON-BLANK override string ⇒ return a copy of the resolved built-in with
      ``model`` set to the override (``dataclasses.replace``), so the caller
      can route this profile's sub-agent to the user-chosen model;
    * ``None`` or a blank / whitespace-only override ⇒ return the built-in
      SINGLETON UNCHANGED (object identity preserved) — a byte-for-byte no-op
      versus before per-profile models existed. This identity guarantee lets
      the ``general`` (or any un-configured) path stay exactly as it was.

    The override is applied on TOP of the resolved profile so a resumed
    session's persisted ``subagent_type`` still selects the right built-in,
    then the current user configuration (re-read each dispatch) supplies the
    model. The domain hard-codes NO model id — the override always comes from
    the adapters/DI layer.
    """
    if not isinstance(name, str):
        base = GENERAL
    else:
        key = name.strip().lower()
        if not key or key == _LEGACY_GENERAL_ALIAS:
            base = GENERAL
        else:
            base = _PROFILES.get(key, GENERAL)
    if isinstance(model_override, str) and model_override.strip():
        return dataclasses.replace(base, model=model_override)
    return base


def list_profiles() -> tuple[AgentProfile, ...]:
    """Return all built-in profiles (GENERAL first) as an immutable tuple."""
    return (GENERAL, EXPLORE)
