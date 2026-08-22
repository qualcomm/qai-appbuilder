# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Rule-based extraction of file / URL / exec references from tool calls.

Two paths, both pure functions with NO I/O:

* **Built-in tools** — the harness ships a fixed catalogue of tools with a
  known argument schema, so we hard-code the ``arg_name → (kind, mode)``
  mapping (see :data:`BUILTIN_TOOL_REFS`). Tools whose arguments carry no
  reference (``web_search``, ``question``, ...) or that need bespoke logic
  (``exec``) are listed in :data:`IGNORED_BUILTIN_TOOLS` so a spurious rule
  never fires on them.
* **MCP tools** — an MCP server declares tools with arbitrary schemas; there
  is no fixed catalogue. The heuristic inspects the tool's declared
  ``inputSchema.properties`` field NAMES against a small set of hints
  (``PATH_FIELD_HINTS`` / ``URL_FIELD_HINTS`` / ``CMD_FIELD_HINTS``) and
  classifies matches as ``file`` / ``url`` / ``exec`` references. The value
  itself is not parsed beyond a size cap (``_MCP_VALUE_MAX_BYTES``) — the
  ledger's own :func:`strip_read_selector` canonicalises paths on ingest.

The single dispatch entry point is :func:`extract`; it never raises so a
call site can safely feed its result into the accumulator without a
``try/except`` per tool call. See CONTEXT-COMPRESSION-NEXT.md §5.2.1 for
the full built-in mapping table.
"""

from __future__ import annotations

import json
from typing import Any

from qai.chat.domain.reference_ledger import FileMode, Reference

__all__ = [
    "BUILTIN_TOOL_REFS",
    "CMD_FIELD_HINTS",
    "IGNORED_BUILTIN_TOOLS",
    "PATH_FIELD_HINTS",
    "URL_FIELD_HINTS",
    "extract",
    "extract_builtin",
    "extract_from_wire_messages",
    "extract_mcp",
]


# --------------------------------------------------------------------------- #
# Built-in tool mapping (§5.2.1)
# --------------------------------------------------------------------------- #

# ``tool_name → [(arg_name, ref_kind, mode)]``. When an entry lists multiple
# tuples the tool contributes one reference per matching argument.
#
# ``mode`` is a :data:`~qai.chat.domain.reference_ledger.FileMode`
# ("R" / "W" / "RW"): "R" for pure reads, "W" for pure writes, "RW" for
# operations that both consult and mutate the target (e.g. ``edit``).
BUILTIN_TOOL_REFS: dict[str, list[tuple[str, str, FileMode]]] = {
    "read": [("path", "file", "R")],
    "list": [("path", "file", "R")],
    "write": [("path", "file", "W")],
    "edit": [("path", "file", "RW")],
    # ``glob`` accepts a pattern that generally contains directory hints;
    # we record it as a weak "R" so the ledger surfaces the search scope
    # without pretending the pattern IS a concrete file path.
    "glob": [("pattern", "file", "R")],
    # ``grep`` reads the file(s) under ``path``; the search pattern itself
    # is content, not a path, and is deliberately ignored.
    "grep": [("path", "file", "R")],
    "web_fetch": [("url", "url", "R")],
}

# Built-in tools that MUST NOT be routed through the generic
# :data:`BUILTIN_TOOL_REFS` mapping. ``exec`` is handled by bespoke logic
# below (the first token of the command is recorded as an ``exec``
# reference); the rest carry no references worth surfacing to the model.
IGNORED_BUILTIN_TOOLS: frozenset[str] = frozenset({
    "exec",
    "web_search",
    "appbuilder_run",
    "todowrite",
    "question",
    "sub_agent",
    "skill",
    "implementation_plan",
})


# --------------------------------------------------------------------------- #
# MCP heuristic hints (§5.2.1)
# --------------------------------------------------------------------------- #

# All hints are matched case-insensitively against the raw argument name.
# Splitting path-like hints out from the write-only names lets us default
# to "R" for the generic ``path`` / ``file`` fields while still promoting
# the explicit output/destination fields to "W".
PATH_FIELD_HINTS: frozenset[str] = frozenset({
    "path",
    "filepath",
    "file",
    "filename",
    "source",
    "input",
})
WRITE_FIELD_HINTS: frozenset[str] = frozenset({
    "target",
    "output",
    "destination",
})
URL_FIELD_HINTS: frozenset[str] = frozenset({"url", "uri", "endpoint"})
CMD_FIELD_HINTS: frozenset[str] = frozenset({"command", "cmd", "argv"})

# Values longer than this (in bytes / chars, whichever the runtime uses)
# are almost certainly not paths / URLs / commands. Skipping them keeps a
# huge inline payload (an LLM prompt fragment, a base64 blob, ...) from
# masquerading as a reference and blowing the ledger's byte budget.
_MCP_VALUE_MAX_BYTES: int = 1024

# ``exec`` (and MCP ``command``-like) values are truncated to this many
# characters before being recorded; the ledger's wire budget is small
# (500 bytes), so a multi-line pipeline would otherwise starve every
# other entry.
_EXEC_VALUE_MAX_CHARS: int = 120


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_builtin(tool_name: str, args: dict[str, Any]) -> list[Reference]:
    """Extract references from a built-in tool call.

    ``exec`` has bespoke handling: the first token of the command line is
    truncated to :data:`_EXEC_VALUE_MAX_CHARS` and recorded as an ``exec``
    reference. Every other :data:`IGNORED_BUILTIN_TOOLS` entry returns an
    empty list.
    """
    if tool_name in IGNORED_BUILTIN_TOOLS:
        if tool_name == "exec":
            cmd = args.get("command") or args.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                return [
                    Reference(
                        kind="exec",
                        value=cmd[:_EXEC_VALUE_MAX_CHARS],
                        mode="R",
                    ),
                ]
        return []
    mapping = BUILTIN_TOOL_REFS.get(tool_name)
    if mapping is None:
        return []
    refs: list[Reference] = []
    for arg_name, kind, mode in mapping:
        value = args.get(arg_name)
        if isinstance(value, str) and value:
            refs.append(Reference(kind=kind, value=value, mode=mode))
    return refs


def extract_mcp(
    tool_name: str,  # noqa: ARG001 — reserved for future per-server rules
    args: dict[str, Any],
    tool_schema: dict[str, Any] | None,
) -> list[Reference]:
    """Extract references from an MCP tool call via schema-name heuristic.

    Returns an empty list when the schema is missing (``None`` / not a
    dict) or lacks a ``properties`` map — we refuse to guess without a
    declared shape. Any value that is not a non-empty string or that
    exceeds :data:`_MCP_VALUE_MAX_BYTES` is skipped so a large inline
    payload cannot masquerade as a reference.
    """
    if not isinstance(tool_schema, dict):
        return []
    props = tool_schema.get("properties")
    if not isinstance(props, dict):
        return []
    refs: list[Reference] = []
    for name, value in args.items():
        if not isinstance(name, str) or name not in props:
            # The value did not correspond to a declared field; ignore it
            # (a call that includes stray keys is the model's problem, not
            # ours).
            continue
        if not isinstance(value, str) or not value:
            continue
        if len(value) >= _MCP_VALUE_MAX_BYTES:
            continue
        name_lower = name.lower()
        if name_lower in WRITE_FIELD_HINTS:
            refs.append(Reference(kind="file", value=value, mode="W"))
        elif name_lower in PATH_FIELD_HINTS:
            refs.append(Reference(kind="file", value=value, mode="R"))
        elif name_lower in URL_FIELD_HINTS:
            refs.append(Reference(kind="url", value=value, mode="R"))
        elif name_lower in CMD_FIELD_HINTS:
            refs.append(
                Reference(
                    kind="exec",
                    value=value[:_EXEC_VALUE_MAX_CHARS],
                    mode="R",
                ),
            )
    return refs


def extract(
    tool_name: str,
    args: dict[str, Any],
    tool_schema: dict[str, Any] | None = None,
    *,
    is_mcp: bool = False,
) -> list[Reference]:
    """Top-level dispatch. Never raises.

    ``is_mcp=True`` routes to :func:`extract_mcp` (schema-name heuristic
    over ``tool_schema.properties``); ``False`` — the default — routes to
    :func:`extract_builtin` (fixed per-tool mapping). Any unexpected error
    surfaces as an empty list so a broken extraction NEVER breaks the turn.
    """
    try:
        if not isinstance(args, dict):
            return []
        if is_mcp:
            return extract_mcp(tool_name, args, tool_schema)
        return extract_builtin(tool_name, args)
    except Exception:  # noqa: BLE001 — extraction is best-effort
        return []


# --------------------------------------------------------------------------- #
# Wire-level extraction (P0-2, Task O)
# --------------------------------------------------------------------------- #
def extract_from_wire_messages(
    wire_messages: "list[dict[str, Any]] | tuple[dict[str, Any], ...]",
) -> list[Reference]:
    """Walk a wire-message list and extract references from every tool call.

    ``_compress_via_checkpoint`` / ``ForceCompactChatUseCase.execute`` /
    the sub-agent's ``_compact_hook`` (``adapters/agent_tool.py``) all need
    the SAME extraction: for every assistant message that carries
    ``tool_calls``, decode each call's ``function.arguments`` JSON and route
    the ``(tool_name, args)`` pair through :func:`extract`. Centralised here
    so the three call sites share one口径 and future MCP-schema wiring lands
    in one place.

    Notes:

    * ``function.arguments`` may be a JSON string (OpenAI-shaped wire) or
      already-decoded ``dict`` (some internal callers pre-decode). Both
      shapes are accepted; a malformed string is silently skipped so a bad
      tool_call never breaks the compaction path.
    * Non-tool_calls messages (``role=user`` / ``role=assistant`` text /
      ``role=tool``) contribute nothing — references live exclusively on the
      assistant's ``tool_calls`` structure.
    * ``is_mcp`` is left at the default ``False``: the wire does NOT carry
      MCP tool schemas, so we route through the built-in mapping. When a
      caller has schema hints it should invoke :func:`extract` directly.
    """
    refs: list[Reference] = []
    for msg in wire_messages:
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get("tool_calls") or ()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if not isinstance(fn, dict):
                continue
            tool_name = fn.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            raw_args: Any = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                continue
            if not isinstance(args, dict):
                continue
            refs.extend(extract(tool_name=tool_name, args=args))
    return refs
