# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared tool-set advertise composition (SUBAGENT-UNIFY-7).

This module is the SINGLE source of truth for the "advertise filter / assemble"
flow that both the main / take-over chat loop
(:meth:`qai.chat.application.use_cases.streaming.StreamChatUseCase._collect_tool_schemas`)
and the autonomous sub-agent dispatch
(:func:`qai.chat.adapters.agent_tool._sub_agent_tool_schemas`) previously
duplicated. Collapsing the two near-identical pipelines into one helper kills
the divergence risk (two copies drifting) and de-duplicates the constants +
the schema-name extractor.

Layering (v2.7 §3.2 / §3.5): this is an ``application``-layer module that
depends ONLY on the stdlib + the chat ``domain`` (``AgentProfile``). It does
**not** import ``adapters`` / ``infrastructure`` / any other context, and it
never imports ``streaming`` (which would form a cycle, since ``streaming``
imports this helper). The ``agent`` tool schema is therefore supplied by the
CALLER via ``agent_schema_factory`` — both callers pass the SAME
``streaming._agent_tool_schema`` (the single source of truth for that schema),
so there is no duplication and no import edge from here into ``streaming``.

The composition order is faithful to the pre-refactor ``agent_tool``
``_sub_agent_tool_schemas`` flow (the more complete of the two):

1. drop the always-excluded names (``excluded``) AND the conditional
   app-builder names (so they can be re-added on the right terms);
2. inject the ``agent`` schema when ``inject_agent`` is set and no ``agent``
   tool is already present (using ``agent_schema_factory``);
3. re-add the conditional app-builder tools ONLY for cloud turns in
   ``app-builder`` mode (``tool_mode == "app-builder" and not is_local``);
4. apply the profile allow/deny policy (``GENERAL`` / ``None`` = no-op) — this
   runs LAST among the set-shaping steps so it ALSO prunes any conditional /
   spawn tool re-added above (parity with the pre-refactor sub-agent flow);
5. drop the per-session ``disabled_tools`` (so the model never even sees a tool
   the user switched off for this conversation).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qai.chat.domain.agent_profile import AgentProfile

# ---------------------------------------------------------------------------
# Shared constants (single source of truth — previously duplicated across
# ``streaming`` and ``agent_tool``).
# ---------------------------------------------------------------------------

# Conditional app-builder tools — advertised ONLY for cloud turns in
# ``app-builder`` mode, IDENTICALLY for the main agent and the sub-agent. V1
# parity: ``backend/tools/_appbuilder_run.py:609`` + ``_appbuilder_batch_run.py``
# register them ``conditional=True`` so ``registry.schemas(exclude_conditional
# =True)`` (the local path) omits them; they are injected as ``extra_tools``
# only for cloud + app-builder turns.
CONDITIONAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"appbuilder_run", "appbuilder_batch_run"}
)

# Tools removed by default from the autonomous sub-agent's advertised set:
#   * ``agent`` — recursion guard (a sub-agent cannot spawn sub-agents unless
#     ``allow_spawn`` un-excludes it together with ``list_subagents``);
#   * ``question`` — its blocking dialog is not reliably reachable from a
#     background sub-agent;
#   * ``list_subagents`` — useless without ``agent`` (un-excluded together with
#     ``agent`` when spawning is permitted).
# The take-over path in ``streaming`` derives its ``excluded`` set from this
# same constant, so the two loops share ONE definition.
SUB_AGENT_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {"agent", "question", "list_subagents"}
)


def schema_tool_name(schema: Any) -> str | None:
    """Extract the tool name from an OpenAI function schema (best-effort).

    Returns ``None`` when *schema* is not a ``dict`` with a ``function`` dict
    carrying a string ``name``. Replaces the two near-identical extractors
    (``streaming._schema_tool_name`` returning ``""`` and ``agent_tool._name``
    returning ``None``) — callers treat ``None``/``""`` equivalently (a missing
    name never matches an exclusion / conditional / disabled name).
    """
    if not isinstance(schema, dict):
        return None
    fn = schema.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    return None


def compose_advertised_tools(
    advertised: Sequence[Any],
    *,
    tool_mode: str | None,
    is_local: bool,
    excluded: frozenset[str],
    inject_agent: bool,
    agent_schema_factory: Callable[[], dict[str, Any]] | None,
    profile: "AgentProfile | None" = None,
    disabled_tools: frozenset[str] = frozenset(),
    enable_skill: bool = True,
) -> list[dict[str, Any]]:
    """Compose the advertised tool schema list shared by both chat loops.

    Parameters
    ----------
    advertised:
        The raw schema list returned by the tool executor's ``schemas()``.
        Non-dict entries are ignored.
    tool_mode:
        The effective tool mode (``"app-builder"`` re-adds the conditional
        app-builder tools for cloud turns).
    is_local:
        ``True`` for on-device model turns — conditional app-builder tools are
        NEVER added for local turns (cloud-only).
    excluded:
        Names always dropped from the base set. The main-agent path passes
        ``frozenset()`` (nothing excluded); the sub-agent / take-over path
        passes :data:`SUB_AGENT_EXCLUDED_TOOLS` minus whatever the active
        ``allow_spawn`` / ``allow_question`` switches un-exclude.
    inject_agent:
        When ``True`` and no ``agent`` tool is already present in the surviving
        set, append ``agent_schema_factory()`` (so the model can request a
        spawn). The main agent always injects; the sub-agent / take-over path
        injects only when spawning is permitted.
    agent_schema_factory:
        Zero-arg factory returning the canonical ``agent`` schema. Supplied by
        the caller (both pass ``streaming._agent_tool_schema``) so this helper
        never imports ``streaming`` (no cycle). Must be non-``None`` whenever
        ``inject_agent`` is ``True``.
    profile:
        Optional per-profile allow/deny policy (sub-agent only). ``None`` /
        ``GENERAL`` is a no-op; ``EXPLORE`` prunes every state-mutating tool.
        Applied LAST so it also drops re-added conditional / spawn tools.
    disabled_tools:
        Per-session ("this conversation only") tool names to drop entirely.
    enable_skill:
        Whether the ``skill`` tool may be advertised. Defaults to ``True`` so
        the main agent and take-over paths (which pass no value) keep the
        ``skill`` tool. The autonomous sub-agent passes ``False`` unless the
        caller granted it an explicit skill whitelist (``enabled_skills`` non-
        empty) — so a default autonomous sub-agent never even sees the ``skill``
        tool (product decision: sub-agents get no skill unless explicitly
        granted). When ``False``, the ``skill`` schema is dropped in Step 1.

    Returns a fresh ``list`` of shallow-copied schema dicts (never aliases the
    input). Returns ``[]`` when *advertised* yields nothing usable.
    """
    # Step 1: base set — drop the always-excluded names AND the conditional
    # app-builder names (re-added below only for cloud + app-builder). When
    # ``enable_skill`` is False also drop the ``skill`` tool (sub-agent with no
    # skill whitelist — the model must not see a tool it may not use).
    out: list[dict[str, Any]] = []
    for s in advertised:
        if not isinstance(s, dict):
            continue
        name = schema_tool_name(s)
        if isinstance(name, str) and (
            name in excluded or name in CONDITIONAL_TOOL_NAMES
        ):
            continue
        if not enable_skill and name == "skill":
            continue
        out.append(dict(s))

    # Step 2: inject the ``agent`` schema when permitted and not already there.
    if inject_agent and not any(
        schema_tool_name(s) == "agent" for s in out
    ):
        if agent_schema_factory is None:
            raise ValueError(
                "agent_schema_factory must be provided when inject_agent=True"
            )
        out.append(agent_schema_factory())

    # Step 3: re-add conditional app-builder tools ONLY for cloud + app-builder.
    if tool_mode == "app-builder" and not is_local:
        for s in advertised:
            if not isinstance(s, dict):
                continue
            name = schema_tool_name(s)
            if isinstance(name, str) and name in CONDITIONAL_TOOL_NAMES:
                out.append(dict(s))

    # Step 4: apply the profile allow/deny policy LAST among set-shaping steps.
    # GENERAL / None keep ``out`` unchanged (regression-safe). Imported here at
    # runtime (application ⇐ domain is the allowed direction) only when a
    # non-trivial profile is in play, so the common path stays import-free.
    if profile is not None:
        from qai.chat.domain.agent_profile import GENERAL

        if profile is not GENERAL:
            present = frozenset(
                n for s in out if isinstance(n := schema_tool_name(s), str)
            )
            permitted = profile.filter_tool_names(present)
            out = [
                s
                for s in out
                if isinstance(n := schema_tool_name(s), str) and n in permitted
            ]

    # Step 5: drop the per-session disabled tools (schema-layer half of the
    # defence; the handler-layer execution gate is the authoritative backstop).
    if disabled_tools:
        out = [
            s
            for s in out
            if not (
                isinstance(n := schema_tool_name(s), str)
                and n in disabled_tools
            )
        ]
    return out


def filter_skill_catalog_by_ids(
    rows: Sequence[Any],
    enabled_ids: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Keep only the skill-catalog rows whose skill id is in *enabled_ids*.

    Shared single source of truth for the enabled-skill WHITELIST used by the
    autonomous sub-agent (``agent_tool``) and the discussion speaker
    (``orchestrate_discussion``) — the main agent does NOT use this (its skill
    catalog is the full globally-enabled set, unchanged). Keeping the id
    derivation here (``basename(dirname(path))``) matches the main-agent
    convention (``streaming`` skill-id extraction) so all paths agree.

    *rows* accepts both ``(path, use_for)`` tuples and ``{"path":..,
    "use_for"/"description":..}`` dicts (the two shapes the cloud / provider
    layers produce). Returns a fresh tuple of ``(path, use_for)`` pairs; an
    empty *enabled_ids* yields ``()`` (no skill — the whitelist default).
    """
    if not enabled_ids:
        return ()
    import os

    out: list[tuple[str, str]] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            path, use_for = str(row[0]), str(row[1])
        elif isinstance(row, dict):
            path = str(row.get("path", ""))
            use_for = str(row.get("use_for") or row.get("description") or "")
        else:
            continue
        if not path:
            continue
        skill_id = os.path.basename(os.path.dirname(path))
        if skill_id in enabled_ids:
            out.append((path, use_for))
    return tuple(out)
