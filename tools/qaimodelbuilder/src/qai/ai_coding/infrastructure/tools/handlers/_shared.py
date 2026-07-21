# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared constants, schemas, and helpers for the tool handler family.

Split out of the original single-file ``handlers.py`` so each tool
family module (``read_write`` / ``search`` / ``exec`` / ``web`` /
``patch`` / ``appbuilder``) can import the common bits without
duplication.  The package ``__init__`` re-exports everything so the
public import path ``qai.ai_coding.infrastructure.tools.handlers``
stays unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.platform.tool_docs import (
    AVAILABLE_TOOLS_SECTION,
    PREFER_DEDICATED_TOOLS_SECTION,
    SHELL_ALIAS_DESCRIPTION,
    SHELL_ALIAS_ENUM,
    SHELL_NOTES_SECTION,
    WORKDIR_GUIDANCE_SECTION,
)

logger = logging.getLogger("qai.ai_coding.tools")


# ---------------------------------------------------------------------------
# Module-level constants (mirroring legacy ``truncation_constants``)
# ---------------------------------------------------------------------------

READ_MAX_LINES = 2000
READ_MAX_BYTES = 50 * 1024  # 50 KB
# Per-line character cap. A file with a few PATHOLOGICALLY long lines (minified
# JS/CSS, base64 blobs, single-line JSON) can blow the context window even when
# the line COUNT is tiny and the total stays under READ_MAX_BYTES on the early
# lines. Capping each emitted line independently protects against that third
# blow-up vector (the existing line-count + byte caps only guard "many lines"
# and "large total"). Over-long lines are cut to this width and tagged so the
# model knows the line was clipped and can re-read a narrower range if it truly
# needs the tail of that line.
READ_MAX_LINE_LENGTH = 2000
#: Generic fallback suffix when the original line length is unknown. Prefer
#: :func:`make_line_truncated_suffix` (gives the model the exact "kept N of M
#: chars" so it knows how much was dropped). Kept as a module-level constant
#: for callers that still want a fixed tag for simple containment checks.
READ_LINE_TRUNCATED_SUFFIX = " ... (line truncated)"


def make_line_truncated_suffix(*, kept_chars: int, original_chars: int) -> str:
    """Build the per-line truncation suffix with exact "kept N of M" counts.

    Used by both ``read`` (file-content lines) and ``grep`` (matched lines) so
    a model that sees a clipped line knows precisely how much it lost — the
    fixed ``" ... (line truncated)"`` tag never said by how much, which left
    the model guessing whether to re-read the line at a different offset or
    accept the head. ``kept_chars`` is the number of chars kept from the
    original line; ``original_chars`` is the full line's character length.

    The output is intentionally one short, model-friendly token block
    (``" ... (line truncated: kept N/M chars)"``) so it tokenises consistently
    and downstream substring checks (e.g. ``"(line truncated" in text``) keep
    working unchanged.
    """
    return (
        f" ... (line truncated: kept {kept_chars}/{original_chars} chars)"
    )
# Maximum file paths the ``glob`` tool shows in-prompt before the list is
# truncated. The complete list is persisted to ``data/tool_results/`` and the
# model can ``read`` it back, so this bounds only the VISIBLE sample, never
# what is recoverable. Results are sorted newest-modified-first before this cut
# so the most recently changed files survive truncation.
GLOB_MAX_RESULTS = 60
# Maximum match LINES the ``grep`` tool shows in-prompt before the result is
# truncated. Matches are ordered by file modification time (newest first)
# before the cut so the most relevant files survive; the complete output is
# persisted + retrievable via ``read``.
GREP_MAX_MATCHES = 100
# Maximum characters of a SINGLE matched line ``grep`` emits before that line's
# text is clipped with an ellipsis marker — a minified / very long matching
# line cannot blow the context window on its own.
GREP_MAX_LINE_LENGTH = 2000
GREP_MAX_OUTPUT_BYTES = 50 * 1024  # 50 KB
# Max bytes of FILE CONTENT the pure-Python grep fallback will read+scan before
# it stops early and flags the result INCOMPLETE. ``grep`` is heavier than
# ``glob``: beyond walking the tree it OPENS and line-scans every candidate
# file, so a directory with very many (or very large) files can make the scan
# phase slow / memory-heavy even when few lines match (the 50 KB OUTPUT cap
# above never trips because nothing matched). This bounds the total content
# read so a pathological scan returns in bounded time with a clear "narrow the
# scope" note instead of grinding. Generous enough that a normal project scan
# never trips it. (The ripgrep backend is bounded by its own subprocess
# timeout instead.)
GREP_MAX_SCAN_BYTES = 256 * 1024 * 1024  # 256 MB of file content read
# Max filesystem entries (dirs + files) a SINGLE recursive ``**`` walk may
# visit before it aborts with a clear "directory too large" error. This is the
# ROOT-CAUSE guard against the "glob hangs for tens of minutes" problem: the
# cost of a recursive walk is driven by HOW MANY entries are traversed, not how
# many match, so a result-count cap (``GLOB_MAX_RESULTS``) alone cannot stop a
# walk that is stat-ing hundreds of thousands of files. The walk checks this
# budget cheaply (O(1)) each entry and stops EARLY — so even a recursive scan
# accidentally rooted at a huge (but not system-root) directory returns in a
# bounded time instead of wedging the turn. The threshold is generous enough
# that a normal project / workspace scan never trips it; only a pathologically
# large tree does. Pairs with the entry-level ``stop_event`` so an upstream
# cancel can also break the walk cooperatively.
WALK_MAX_ENTRIES = 200_000
# NOTE: exec has NO default timeout (V1/v0.5 parity — omitting ``timeout``
# means run to completion; see ``exec._resolve_timeout``; only an explicit
# positive value caps the run) and NO inline preview truncation at decode time
# (the full output is persisted by the ``ToolResultStorePort`` which renders a
# small head+tail preview — see ``tool_result_store`` + registry
# ``_apply_result_store``). The former ``EXEC_DEFAULT_TIMEOUT_SECONDS`` and the
# inline preview-cap constant were removed once those behaviours moved.
#
# ``EXEC_MAX_OUTPUT_BYTES`` below is a DIFFERENT thing: a hard memory-safety cap
# on how many bytes we will read from a child's stdout/stderr pipes before we
# KILL it. Without it, a runaway command (e.g. ``dir /s C:\`` / ``cat`` a huge
# binary / an infinite-output loop) would have its multi-GB output read fully
# into memory by ``communicate()`` BEFORE the store ever truncates it — an OOM
# risk. The cap is deliberately GENEROUS (well above any realistic legitimate
# output) so it never clips a normal build/install log; it only fires on a
# pathological flood, in which case we keep the head we read, mark the result
# truncated, and kill the process. This is NOT a timeout and does NOT change the
# "omit = no timeout" V1 parity — a slow-but-quiet command still runs to
# completion.
EXEC_MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # 64 MB hard cap (OOM guard, not a cut)
WEBFETCH_DEFAULT_MAX_CHARS = 20_000


# ---------------------------------------------------------------------------
# In-prompt size caps (centralised, runtime-configurable)
#
# The constants above are the BUILT-IN DEFAULTS. The thresholds that decide
# how much of a tool's result is shown to the model in-prompt are gathered
# into one :class:`ToolOutputThresholds` value object so they can be tuned
# from a single config surface (``settings.tool_output``) and threaded into
# the handlers via a module-level seam — mirroring the existing
# ``set_project_skip_dirs`` / ``set_ssl_verify`` config seams. The handlers
# read the live thresholds through :func:`get_tool_output_thresholds`; the
# ``apps/api`` wiring root installs the user-configured values via
# :func:`set_tool_output_thresholds` at tool-bridge build time. When nothing
# is installed the defaults equal the module constants, so behaviour is
# byte-for-byte unchanged for any caller that does not wire config.
#
# Two cap families:
#   * result-count caps (``glob_max_results`` / ``grep_max_matches``) bound a
#     structured list of entries (re-fetchable with a tighter pattern);
#   * line / byte / length caps bound contiguous text (paired with on-disk
#     persistence + ``read(offset=…)`` recovery).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolOutputThresholds:
    """Immutable bundle of the in-prompt size caps for the tool family.

    Defaults equal the module-level constants so an unconfigured deployment
    behaves exactly as before. Constructed once by the wiring root from
    ``settings.tool_output`` and installed via
    :func:`set_tool_output_thresholds`.
    """

    read_max_lines: int = READ_MAX_LINES
    read_max_bytes: int = READ_MAX_BYTES
    read_max_line_length: int = READ_MAX_LINE_LENGTH
    glob_max_results: int = GLOB_MAX_RESULTS
    grep_max_matches: int = GREP_MAX_MATCHES
    grep_max_line_length: int = GREP_MAX_LINE_LENGTH
    grep_max_output_bytes: int = GREP_MAX_OUTPUT_BYTES


_TOOL_OUTPUT_THRESHOLDS: list[ToolOutputThresholds] = [ToolOutputThresholds()]


def set_tool_output_thresholds(thresholds: ToolOutputThresholds | None) -> None:
    """Install the in-prompt size caps used by the tool handlers.

    Called by ``apps/api`` from ``settings.tool_output`` at tool-bridge build
    time. ``None`` resets to the built-in defaults (the module constants).
    """
    _TOOL_OUTPUT_THRESHOLDS[0] = thresholds or ToolOutputThresholds()


def get_tool_output_thresholds() -> ToolOutputThresholds:
    """Return the currently-installed in-prompt size caps (defaults if unset)."""
    return _TOOL_OUTPUT_THRESHOLDS[0]


CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".cpp", ".c", ".h", ".hpp",
    ".java", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt",
    ".sh", ".bat", ".ps1", ".lua", ".r", ".m", ".scala", ".dart",
}

DEFAULT_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".venv", "venv", "env", ".env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist",
    "node_modules", ".next", ".nuxt", ".svelte-kit",
    ".git", ".hg", ".svn",
    ".idea", ".vscode",
})


# ---------------------------------------------------------------------------
# Public schemas (OpenAI function-calling format; consumed by the agent
# harness in PR-108 and the route layer for tool list endpoints)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "type": "function",
        "function": {
            "name": "read",
            # V1 parity (backend/tools/_read.py:218-225) for the truncation /
            # offset recovery guidance — on-device models rely on the "use the
            # suggested offset rather than guessing" hint to continue truncated
            # reads correctly.
            #
            # DELIBERATE DEVIATION from V1 (AGENTS.md "对齐 V1 前先评估其合理性"):
            # V1's wording "Supports text files and images (jpg, png, gif,
            # webp)" is a net-negative for the on-device chat path — local
            # runtimes (Qwen3-8b etc.) are NOT multimodal, so advertising image
            # support only lures the model into ``read``-ing binary files
            # (images / executables / archives), flooding the context with
            # unusable base64 / garbage and wasting tokens. We replace it with
            # an explicit text-only directive + binary-file warning. This is an
            # enhancement (fixes a V1 defect), not a behaviour regression: the
            # tool's truncation / offset semantics are unchanged.
            "description": (
                "Read the contents of a TEXT file. Do NOT use this on binary "
                "files (images, executables, archives, etc.) — it wastes "
                "context and produces unusable output. By default this tool "
                "returns up to 2000 lines from the start of the file; output is "
                "truncated to 2000 lines or 50KB. On truncation the response "
                "includes the exact line range read, total line count, and the "
                "offset to use for continuing — read the truncation notice "
                "carefully and use the suggested offset rather than guessing. "
                "Use the 'grep' tool to find specific content in large files "
                "before reading them. Avoid tiny repeated slices (e.g. 30-line "
                "chunks); if you need more context, read a larger window. Call "
                "this tool in parallel when you know you want to read multiple "
                "files."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file to read (relative or absolute)"
                        ),
                    },
                    "offset": {
                        "type": "number",
                        "description": (
                            "The line number to start reading from (1-indexed)"
                        ),
                    },
                    "limit": {
                        "type": "number",
                        "description": (
                            "The maximum number of lines to read (defaults to "
                            "2000). Widen it to read a larger window in one "
                            "call rather than issuing many small reads."
                        ),
                    },
                },
            },
        },
    },
    "list": {
        "type": "function",
        "function": {
            "name": "list",
            "description": (
                "When exploring an unfamiliar codebase, prefer 'glob'/'grep' "
                "to jump straight to the relevant files rather than listing "
                "directories exhaustively. Use 'list' when you specifically "
                "need to see the direct contents of ONE directory — a single "
                "level, NOT recursive — for example to discover empty "
                "sub-directories that the files-only 'glob' does not return. "
                "Sub-directory names are suffixed with '/' and entries are "
                "sorted alphabetically. Output is paginated: it shows up to "
                "2000 entries from 'offset' (1-indexed); when more remain the "
                "response says how many and which 'offset' to pass next."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the directory to list (relative or "
                            "absolute)"
                        ),
                    },
                    "offset": {
                        "type": "number",
                        "description": (
                            "Entry number to start listing from (1-indexed)"
                        ),
                    },
                    "limit": {
                        "type": "number",
                        "description": "Maximum number of entries to return",
                    },
                },
            },
        },
    },
    "write": {
        "type": "function",
        "function": {
            "name": "write",
            # V1 parity (backend/tools/_write.py:48-51).
            "description": (
                "Write content to a file (complete overwrite only). Creates "
                "the file if it doesn't exist. For partial edits use the "
                "'edit' tool."
            ),
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file to write (relative or absolute)"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete content to write to the file",
                    },
                },
            },
        },
    },
    "edit": {
        "type": "function",
        "function": {
            "name": "edit",
            # V1 parity (backend/tools/_edit.py:76-79), plus whitespace-tolerant
            # matching and an optional replaceAll (tail-added; back-compatible).
            "description": (
                "Edit a single file using text replacement. Each "
                "edits[].oldText must match a unique region of the file; "
                "matching tolerates line-ending (CRLF/LF) and "
                "leading/trailing whitespace differences. Set replaceAll to "
                "replace every occurrence instead of requiring a unique match."
            ),
            "parameters": {
                "type": "object",
                "required": ["path", "edits"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file to edit (relative or absolute)"
                        ),
                    },
                    "edits": {
                        "type": "array",
                        "description": "One or more targeted replacements",
                        "items": {
                            "type": "object",
                            "required": ["oldText", "newText"],
                            "properties": {
                                "oldText": {
                                    "type": "string",
                                    "description": (
                                        "Text to find. Must be unique in the "
                                        "file unless replaceAll is set; "
                                        "line-ending and surrounding "
                                        "whitespace differences are tolerated."
                                    ),
                                },
                                "newText": {
                                    "type": "string",
                                    "description": "Replacement text",
                                },
                                "replaceAll": {
                                    "type": "boolean",
                                    "description": (
                                        "Replace every exact occurrence of "
                                        "oldText instead of requiring a unique "
                                        "match. Defaults to false."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "glob": {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "Find FILES matching a glob pattern (e.g. '**/*.py', "
                "'src/*.ts'). Returns up to 100 file paths, ordered with the "
                "most-recently-modified files first; when more match, the "
                "complete list is saved and retrievable via read(path=...). "
                "Only files are returned (directories themselves are not "
                "listed). When doing an open-ended search that "
                "may require multiple rounds, delegate to a sub-agent (the "
                "'agent' tool). You can call multiple search tools in parallel "
                "as a batch."
            ),
            "parameters": {
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string"},
                    "cwd": {"type": "string"},
                },
            },
        },
    },
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search for a regex pattern across files. Returns matching "
                "lines with file path and 1-based line number. Use this tool "
                "when you need to find files containing specific patterns. When "
                "doing a deep search that may require multiple tool "
                "invocations, delegate to a sub-agent (the 'agent' tool) to "
                "keep the main context small."
            ),
            "parameters": {
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                    "ignoreCase": {"type": "boolean"},
                    "contextLines": {"type": "number"},
                },
            },
        },
    },
    "exec": {
        "type": "function",
        "function": {
            "name": "exec",
            # V1 parity (backend/tools/_exec.py:1556-1575): keep the FULL
            # operator-manual description verbatim. On-device models rely on
            # the PortableGit Unix-tool catalogue and the "prefer Unix tools /
            # dedicated tools over cmd built-ins" guidance to generate commands
            # that actually run cleanly in this environment; the prior 1-line
            # summary dropped all of it and made the model reach for less
            # reliable cmd built-ins (del / Remove-Item) or miss the available
            # Unix tools entirely.
            #
            # Historical note (2026-07-01 Windows ACL / AppContainer cleanup):
            # earlier revisions of this description said commands "run inside
            # an AppContainer sandbox" and listed cmd built-ins as forbidden.
            # The AppContainer / LPAC sandbox execution chain was removed
            # (see ``docs/85-tasks/windows-acl-sandbox-cleanup-2026-07-01.md``);
            # cmd built-ins now work, but we still steer the model toward
            # PortableGit Unix tools / dedicated file tools because they are
            # cross-shell portable, produce better output, and are the
            # long-term safe defaults.
            #
            # 2026-07-13: extracted shared fragments (AVAILABLE_TOOLS_SECTION,
            # SHELL_NOTES_SECTION, WORKDIR_GUIDANCE_SECTION,
            # PREFER_DEDICATED_TOOLS_SECTION, SHELL_ALIAS_ENUM / _DESCRIPTION)
            # to ``qai.platform.tool_docs`` so ``exec`` and
            # ``background_process`` share a single source of truth for the
            # environment/shell surface. Tool-specific pieces (one-shot
            # semantics, timeout, cwd) stay here.
            "description": (
                "Execute a one-shot command on the Windows system. Supports "
                "cmd.exe, PowerShell 5.1, and the PortableGit Unix shell "
                "(sh/bash). The shell is auto-detected by default: commands "
                "with PowerShell syntax (& call operator, cmdlets like "
                "Get-ChildItem, $variables) use powershell.exe; otherwise "
                "cmd.exe is used. Set shell='sh' to use the PortableGit "
                "Unix shell explicitly.\n\n"
                f"{AVAILABLE_TOOLS_SECTION}\n\n"
                f"{PREFER_DEDICATED_TOOLS_SECTION}\n\n"
                f"{WORKDIR_GUIDANCE_SECTION}\n\n"
                f"{SHELL_NOTES_SECTION}\n\n"
                "When invoking this tool, also fill the 'description' arg "
                "with a brief phrase (a few words) naming the intent — the "
                "chat UI renders it as the tool-card subtitle so the user "
                "can follow along without having to expand every command."
            ),
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command string to execute",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "A brief phrase naming what this call is for — "
                            "shown as the tool-card subtitle in the chat so "
                            "the user can follow the assistant's work at a "
                            "glance without expanding each command. Aim for "
                            "an action-first phrase (a few words is plenty), "
                            "e.g. 'Install project dependencies', 'Run unit "
                            "tests', 'List Python processes'. Optional but "
                            "strongly encouraged for anything beyond a "
                            "trivial one-liner."
                        ),
                    },
                    "shell": {
                        "type": "string",
                        "enum": list(SHELL_ALIAS_ENUM),
                        "description": SHELL_ALIAS_DESCRIPTION,
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Optional timeout in seconds (kills the process on "
                            "expiry). OMIT (or pass 0) to run with NO timeout — "
                            "the command waits to completion (use this for long "
                            "builds / installs). Only a positive value caps the "
                            "run."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory for the command. Defaults to the "
                            "project root. Use this instead of 'cd' inside the "
                            "command."
                        ),
                    },
                },
            },
        },
    },
    "webfetch": {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": (
                "Fetch and extract readable content from a URL "
                "(markdown by default, plain text optionally)."
            ),
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string"},
                    "extractMode": {
                        "type": "string",
                        "enum": ["markdown", "text"],
                    },
                    "maxChars": {"type": "number"},
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Request timeout in seconds (default 30, capped "
                            "at 120). Increase for a slow endpoint."
                        ),
                    },
                },
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web / an internal knowledge base for a query and "
                "return a ranked list of results (title, url, snippet, score). "
                "Use this to find documents or pages relevant to a question; "
                "follow up with `webfetch` on a result url to read its full "
                "content."
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string.",
                    },
                    "count": {
                        "type": "number",
                        "description": (
                            "Maximum number of results to return (default 5)."
                        ),
                    },
                    "provider": {
                        "type": "string",
                        "description": (
                            "Optional search backend id. Omit to use the "
                            "default provider."
                        ),
                    },
                },
            },
        },
    },
    "apply_patch": {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a multi-file patch atomically. Format wraps "
                "*** Begin Patch / *** End Patch around Add / Update / "
                "Delete File: directives."
            ),
            "parameters": {
                "type": "object",
                "required": ["patch"],
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": (
                            "The complete patch text, wrapped in "
                            "*** Begin Patch ... *** End Patch."
                        ),
                    }
                },
            },
        },
    },
    "appbuilder_run": {
        "type": "function",
        "function": {
            "name": "appbuilder_run",
            "description": (
                "Run an on-device App Builder Model Pack for specialized AI "
                "inference on the NPU/CPU (via QNN). Use this when the user "
                "asks to run an installed Model Pack such as speech "
                "recognition (ASR), speech synthesis (TTS), or optical "
                "character recognition (OCR). Provide the pack id in "
                "'modelId' and the pack-specific input data in 'inputs'. "
                "Returns the inference result on success. If no matching "
                "Model Pack is installed on the device, it returns an error "
                "with an 'error_code' instead of a result; in that case, "
                "tell the user the pack is not available and do not retry."
            ),
            "parameters": {
                "type": "object",
                "required": ["modelId", "inputs"],
                "properties": {
                    "modelId": {
                        "type": "string",
                        "description": (
                            "Id of the installed App Builder Model Pack to "
                            "run, as listed in the model catalog. Required."
                        ),
                    },
                    "inputs": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Pack-specific input payload. The expected keys "
                            "depend on the pack type (e.g. audio data for "
                            "ASR, text for TTS, image data for OCR); consult "
                            "the pack's catalog entry for the exact schema. "
                            "Required."
                        ),
                    },
                    "params": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Optional tuning parameters for this run (e.g. "
                            "sampling or decoding options). Unlike 'inputs', "
                            "these adjust how the pack processes the data "
                            "rather than what data to process. Omit to use "
                            "the pack defaults."
                        ),
                    },
                    "variantId": {
                        "type": "string",
                        "description": (
                            "Optional id of a specific variant of the Model "
                            "Pack (e.g. a different size or precision build). "
                            "Omit to use the pack's default variant."
                        ),
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Cloud-model tool description overrides
#
# The on-device (local NPU) chat path relies on the SHORT, stable descriptions
# in ``TOOL_SCHEMAS`` above — small local runtimes (Qwen3-8b etc.) behave
# better with terse tool docs and MUST NOT be perturbed. Cloud models
# (Claude / Sonnet / GPT-class), by contrast, follow richer procedural
# guidance well and benefit from the same "read-before-edit / grep-first /
# no-`&&`-in-PowerShell" steering that agentic coding assistants use.
#
# ``streaming.py``'s ``_collect_tool_schemas`` (owned by a sibling change)
# consults this table when assembling the CLOUD ``tools`` payload: for each
# tool present here it overlays ``description`` onto ``function.description``
# and, when ``param_descriptions`` is given, overlays each entry onto the
# matching ``function.parameters.properties[<name>].description``. Tools NOT
# listed here keep their ``TOOL_SCHEMAS`` description unchanged on both paths.
#
# HARD invariants (do NOT break):
#   * ``TOOL_SCHEMAS`` above is the local truth and is never mutated here.
#   * Tool ``name`` / parameter field names / ``required`` lists are contract-
#     locked — this table only rewrites human-readable ``description`` strings,
#     never structure.
#   * Wording is our own (semantically aligned with mainstream agentic-coding
#     guidance, but not copied verbatim from any external tool). English, since
#     cloud models follow English procedural instructions most reliably.
#
# ``param_descriptions`` is optional; omit it for tools that only need a richer
# top-level ``description``.
# ---------------------------------------------------------------------------

CLOUD_TOOL_DESCRIPTION_OVERRIDES: dict[str, dict[str, Any]] = {
    "read": {
        "description": (
            "Read the contents of a TEXT file. By default this returns up to "
            "the first 2000 lines (capped at 2000 lines or 50KB); when the "
            "file is longer the response reports the exact line range read, "
            "the total line count, and the offset to resume from — read that "
            "notice and pass the suggested offset rather than guessing. "
            "Prefer to locate what you need FIRST with the 'grep' tool, then "
            "read only the target region instead of pulling the whole file "
            "into context. Do not make many tiny overlapping reads (e.g. "
            "repeated 30-line slices); when you need more surrounding "
            "context, widen the window and read a larger block in one call. "
            "You may call 'read' in parallel when you already know several "
            "files you want to open. Do NOT point this at binary files "
            "(images, executables, archives) — the output is unusable and "
            "wastes context; use a dedicated tool for those instead. Use "
            "offset/limit to page through or resume a truncated read."
        ),
        "param_descriptions": {
            "offset": (
                "1-indexed line number to start reading from — use the value "
                "the truncation notice suggests to continue a long file."
            ),
            "limit": (
                "Maximum number of lines to return in this call (widen it "
                "when you need a larger window rather than issuing many small "
                "reads)."
            ),
        },
    },
    "list": {
        "description": (
            "When getting oriented in an unfamiliar codebase, reach for "
            "'glob'/'grep' to jump straight to the relevant files instead of "
            "walking directories level by level. Use 'list' when you "
            "specifically need to inspect the direct contents of ONE "
            "directory — a single level, NOT recursive — e.g. to see empty "
            "sub-directories that the files-only 'glob' tool omits. "
            "Sub-directory names end with '/' and entries are sorted "
            "alphabetically; output pages from 'offset' (1-indexed) and "
            "reports the next offset when more remain."
        ),
    },
    "write": {
        "description": (
            "Create a file or completely overwrite an existing one. Before "
            "overwriting a file that already exists, 'read' its current "
            "contents first so you do not silently discard work you needed "
            "to keep. Prefer the 'edit' tool for changing an existing file; "
            "only create a new file when it is genuinely necessary. Do not "
            "proactively create documentation files (*.md, README, etc.) "
            "unless the user explicitly asks for them, and do not add emoji "
            "to files unless the user requests it."
        ),
    },
    "edit": {
        "description": (
            "Make targeted text replacements inside a SINGLE existing file. "
            "You MUST 'read' the file at least once earlier in the "
            "conversation before editing it — an edit on an unread file is "
            "rejected. IMPORTANT: the 'read' tool prints every line with an "
            "'N: ' prefix (line number + colon + space); that prefix is NOT "
            "part of the file. Your oldText must contain ONLY the real file "
            "text that comes AFTER that prefix — never include the "
            "line-number prefix in oldText, which is the single most common "
            "cause of a failed match. If oldText is not found, the edit "
            "fails: re-read the file and copy the target text exactly, "
            "preserving its precise indentation (tabs vs spaces must match). "
            "If oldText appears more than once, the edit also fails — either "
            "add enough surrounding context to make it unique, or set "
            "replaceAll to change every occurrence (ideal for renaming a "
            "symbol or variable throughout the file). Always prefer editing "
            "an existing file over creating a new one."
        ),
    },
    "glob": {
        "description": (
            "Find FILES matching a glob pattern (e.g. '**/*.py', "
            "'src/*.ts'). Returns up to 100 paths, most-recently-modified "
            "first; when more match, the full list is saved and retrievable "
            "via read(path=...). Only files are returned (directories "
            "themselves are not listed). For open-ended exploration "
            "that will take several rounds of searching, hand the job to a "
            "sub-agent (the 'agent' tool) so the main context stays focused. "
            "You can also fire off several search tools in parallel in one "
            "batch."
        ),
    },
    "grep": {
        "description": (
            "Search file contents by regular expression and return matching "
            "lines as 'path:line'. Reach for this whenever you need to find "
            "where a pattern occurs across the codebase. When a search will "
            "need many rounds of digging, delegate it to a sub-agent (the "
            "'agent' tool) to keep the main context small. If you only need "
            "to COUNT how many matches exist, run rg (ripgrep) through the "
            "'exec' tool instead of using this tool."
        ),
    },
    "exec": {
        # Cloud variant: keep the PortableGit Unix-tool catalogue + the
        # "prefer Unix tools / dedicated tools" guidance (shared with the
        # on-device variant), then APPEND cloud-only procedural steering
        # (prefer dedicated file tools, PowerShell 5.1 has no ``&&``,
        # truncation-to-file recovery, cwd).
        #
        # Historical note (2026-07-01 Windows ACL / AppContainer cleanup):
        # earlier revisions of this description said commands "run in an
        # AppContainer sandbox" and listed cmd built-ins as forbidden.  The
        # AppContainer / LPAC sandbox execution chain was removed (see
        # ``docs/85-tasks/windows-acl-sandbox-cleanup-2026-07-01.md``);
        # cmd built-ins now work, but we still steer the cloud model toward
        # PortableGit Unix tools / dedicated file tools for the same
        # portability + output-quality reasons as the on-device variant.
        #
        # 2026-07-13: rebuilt on top of the shared ``qai.platform.tool_docs``
        # fragments (AVAILABLE_TOOLS_SECTION / PREFER_DEDICATED_TOOLS_SECTION
        # / WORKDIR_GUIDANCE_SECTION / SHELL_NOTES_SECTION) so a change to
        # the environment catalogue or shell guidance propagates to BOTH the
        # on-device variant and the cloud override in a single edit. Cloud-
        # only additions (truncation-to-file behaviour, `&&` reminder) stay
        # here as extra paragraphs.
        "description": (
            "Execute a one-shot command on Windows. Supports cmd.exe, "
            "PowerShell 5.1, and the PortableGit Unix shell (sh/bash). The "
            "shell is auto-detected by default: commands with PowerShell "
            "syntax (& call operator, cmdlets like Get-ChildItem, "
            "$variables) will automatically use powershell.exe; otherwise "
            "cmd.exe is used. Set shell='sh' to use the PortableGit Unix "
            "shell explicitly.\n\n"
            f"{AVAILABLE_TOOLS_SECTION}\n\n"
            f"{PREFER_DEDICATED_TOOLS_SECTION}\n\n"
            f"{WORKDIR_GUIDANCE_SECTION}\n\n"
            f"{SHELL_NOTES_SECTION}\n\n"
            "TRUNCATION: over-limit output is truncated and the full text "
            "written to a file (the notice explains how to retrieve it) — "
            "do NOT shorten output yourself with head / tail / more.\n\n"
            "When invoking this tool, also fill the 'description' arg with "
            "a brief phrase (a few words) naming the intent — the chat UI "
            "renders it as the tool-card subtitle so the user can follow "
            "along without having to expand every command."
        ),
        "param_descriptions": {
            "cwd": (
                "Working directory for the command. Prefer this over an "
                "in-command cd / Set-Location."
            ),
            "shell": SHELL_ALIAS_DESCRIPTION,
            "description": (
                "A brief phrase naming what this call is for — shown as "
                "the tool-card subtitle in the chat so the user can follow "
                "the assistant's work at a glance without expanding each "
                "command. Aim for an action-first phrase (a few words is "
                "plenty), e.g. 'Install project dependencies', 'Run unit "
                "tests', 'List Python processes'. Optional but strongly "
                "encouraged for anything beyond a trivial one-liner."
            ),
        },
    },
    "webfetch": {
        "description": (
            "Fetch a URL and extract its readable content (markdown by "
            "default, or plain text). When the page is large the returned "
            "content is truncated; the truncation notice explains how to "
            "get more — raise 'maxChars' or fetch a more specific "
            "deep-linked URL."
        ),
        "param_descriptions": {
            "url": (
                "The fully-formed http/https URL to fetch content from."
            ),
            "extractMode": (
                "Output format: 'markdown' (default, best for most pages) "
                "or 'text' (plain text)."
            ),
            "maxChars": (
                "Maximum characters of extracted content to return (default "
                "20000). Increase to retrieve more of a large page."
            ),
        },
    },
    "apply_patch": {
        "description": (
            "Apply one or more file changes atomically using a structured "
            "patch format.\n\n"
            "Format:\n"
            "  *** Begin Patch\n"
            "  *** Add File: path/to/new_file.py\n"
            "  + first line of new file\n"
            "  + second line\n"
            "  *** Update File: path/to/existing.py\n"
            "  @@ context line near the change\n"
            "  - line to remove\n"
            "  + line to add\n"
            "  *** Delete File: path/to/old_file.py\n"
            "  *** End Patch\n\n"
            "Rules:\n"
            "- The whole patch must be wrapped in the '*** Begin Patch' / "
            "'*** End Patch' markers, and every operation must start with "
            "an 'Add File:' / 'Update File:' / 'Delete File:' header.\n"
            "- For Add File: prefix every content line with '+'.\n"
            "- For Update File: use '@@ <context>' to anchor where the "
            "change goes, then '-' lines to remove and '+' lines to add; "
            "unchanged context lines start with a single leading space.\n"
            "- To rename/move a file: add '*** Move to: new/path' on the line "
            "right after '*** Update File: old/path' (the hunks then apply to "
            "the old file's content and the result is written to the new "
            "path, with the old path removed). The new path must not already "
            "exist.\n"
            "- For Delete File: no content lines are needed.\n"
            "- Do not operate on the same path more than once in one patch."
        ),
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(message: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **fields}


# ---------------------------------------------------------------------------
# Truncation recovery advice (V1 parity: ``truncation_constants.py`` 7 funcs)
#
# When a tool's output is truncated the model otherwise sees a bare
# ``[content truncated]`` marker and cannot tell what to do next.  These
# helpers each return an actionable ``[truncation note]`` suffix that tells
# the model how to recover (offset+limit / tighter pattern / smaller scope /
# bigger maxChars / redirect-to-file).  Mirrors the legacy
# ``backend/truncation_constants.py`` ``make_*_advice`` family so the wording
# stays consistent across every truncation point.
#
# Wording is intentionally English: most LLMs respond more reliably to
# English recovery instructions.  The fixed ``[truncation note]`` tag lets
# the model recognise this as metadata rather than file content.
# ---------------------------------------------------------------------------

# Shared core recovery action (kept as a constant so every advice string
# stays in sync).
_GENERIC_RECOVERY_ADVICE = (
    "If the visible head/tail above already contains the data you need, "
    "use it directly. Otherwise, re-run the command with output redirected "
    "to a file (e.g. `> out.txt 2>&1`), then extract specific lines with "
    "grep / findstr / sed / `read` tool with offset+limit."
)


def make_truncation_advice(reason: str = "") -> str:
    """Generic truncation advice (exec / storage / channel scenarios)."""
    if reason:
        head = f"[truncation note] Output was truncated ({reason})."
    else:
        head = (
            "[truncation note] Output was truncated due to context "
            "window limits."
        )
    return f"{head} {_GENERIC_RECOVERY_ADVICE}"


def make_grep_advice(cap_kb: int = 50) -> str:
    """grep truncation advice — narrow the search / tighten the pattern."""
    return (
        f"[truncation note] Grep output exceeded {cap_kb}KB. "
        "If the visible matches above already contain what you need, use "
        "them directly. Otherwise: try a more specific `pattern`, narrow "
        "`path` to a smaller scope, or add an `include` filter to fit more "
        "files."
    )


def make_glob_advice(max_results: int) -> str:
    """glob truncation advice — refine the pattern."""
    return (
        f"[truncation note] Glob matched more than {max_results} files. "
        "If the visible files above already contain what you need, use them "
        "directly. Otherwise: tighten the `pattern` (e.g. add a subdirectory "
        "prefix or stricter extension filter) or split the search into "
        "smaller scopes."
    )


def make_webfetch_advice(limit: int) -> str:
    """webfetch truncation advice — raise maxChars or deep-link the URL."""
    return (
        f"[truncation note] Web page content exceeded {limit} chars. "
        "If the visible head above already contains what you need, use it "
        f"directly. Otherwise: increase `maxChars` (e.g. {limit * 2}), or "
        "fetch a more specific URL (deep-linked anchor / API endpoint) "
        "instead of the full page."
    )


def make_web_search_advice(limit: int) -> str:
    """web_search truncation advice — narrow query or fetch a result url."""
    return (
        f"[truncation note] Web search returned more than {limit} results. "
        "If the visible results above already contain what you need, use them "
        "directly. Otherwise: narrow the `query` with more specific terms, or "
        f"raise `count` (e.g. {limit * 2}) to see more hits. To read a "
        "result's full content, call `webfetch` on its url."
    )


def make_file_broker_advice(max_entries: int) -> str:
    """file_broker list-truncation advice — narrow scope or raise cap."""
    return (
        f"[truncation note] File enumeration exceeded {max_entries} entries. "
        "If the visible entries above already contain what you need, use "
        "them directly. Otherwise: narrow the search path / pattern, or "
        f"raise `file_broker.max_entries` (current: {max_entries})."
    )


def make_read_advice(next_offset: int) -> str:
    """read truncation advice — continue with the next ``offset``."""
    return (
        "[truncation note] Read output was truncated. If the visible lines "
        "above already contain what you need, use them directly. Otherwise: "
        f"call `read` again with offset={next_offset} to continue, or pass a "
        "larger `limit` to read a bigger window."
    )


def make_exec_advice(cap_kb: int = 200) -> str:
    """exec truncation advice — redirect output then extract."""
    return (
        f"[truncation note] Command output exceeded {cap_kb}KB and was "
        "truncated. " + _GENERIC_RECOVERY_ADVICE
    )


# ---------------------------------------------------------------------------
# ANSI / VT100 escape stripping (V1 parity: ``text_normalize.strip_ansi``)
#
# CLI programs that use colorama / rich / tqdm often still emit colour /
# cursor escapes even when stdout is piped (they fail to detect the
# redirect).  Literal escape bytes in tool output confuse the model
# (mistaking ``[32m`` for content boundaries, wasting tokens, breaking
# downstream grep reasoning).  We strip them before returning text to the
# model — colour has no semantic value to a terminal-less LLM.
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b                # ESC byte
    (?:
        \[              # CSI introducer
        [0-?]*          # parameter bytes 0x30-0x3F
        [ -/]*          # intermediate bytes 0x20-0x2F
        [@-~]           # final byte 0x40-0x7E
      |
        \]              # OSC introducer
        [^\x07\x1b]*    # any chars except BEL or ESC
        (?:\x07|\x1b\\) # terminator BEL or ESC backslash
      |
        [@-_]           # other 7-bit ESC sequences (ESC = ESC > ESC c ...)
    )
    """,
    re.VERBOSE,
)


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI/VT100 escape sequences from a captured-output string.

    Fast-paths the common case (no ESC byte present) so the regex pass cost
    is negligible — most tool output contains no escape byte at all.
    """
    if "\x1b" not in text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def _format_truncation_notice(
    *, start_line: int, end_line: int, total_lines: int, reason: str
) -> str:
    remaining = max(0, total_lines - end_line)
    is_user = reason.startswith("user limit")
    label = "info" if is_user else "read truncated"
    if remaining > 0:
        next_off = end_line + 1
        return (
            f"\n...[{label}: {reason}; "
            f"showed lines {start_line}-{end_line} of total {total_lines}; "
            f"{remaining} more line(s) available — call read again with "
            f"offset={next_off} to continue]\n"
        )
    return (
        f"\n...[{label}: {reason}; "
        f"showed lines {start_line}-{end_line} of total {total_lines}]\n"
    )


def _is_skip_dir(
    path: Path, extra_skip: frozenset[str] | None = None
) -> bool:
    name = path.name
    if name in DEFAULT_SKIP_DIR_NAMES:
        return True
    if extra_skip and name.lower() in extra_skip:
        return True
    if name.endswith(".egg-info"):
        return True
    return (path / "pyvenv.cfg").exists()


def _is_system_root(path: Path) -> bool:
    resolved = path.resolve()
    if sys.platform == "win32":
        return resolved == resolved.parent
    return str(resolved) == "/"


def _check_recursive_root_guard(root: Path, pattern: str, tool: str) -> None:
    if _is_system_root(root) and "**" in pattern:
        raise ToolError(
            f"{tool}: refusing to run recursive pattern {pattern!r} on "
            f"system root {root!r} — would traverse the entire drive."
        )


def _expand_braces(pattern: str) -> list[str]:
    if "{" not in pattern:
        return [pattern]
    brace_re = re.compile(r"\{([^{}]+)\}")
    results = [pattern]
    while True:
        expanded: list[str] = []
        found = False
        for p in results:
            m = brace_re.search(p)
            if m:
                found = True
                prefix = p[: m.start()]
                suffix = p[m.end() :]
                for alt in m.group(1).split(","):
                    expanded.append(prefix + alt.strip() + suffix)
            else:
                expanded.append(p)
        if not found:
            break
        results = expanded
    return results


@dataclass
class WalkBudget:
    """Bounds + cooperative-cancel state for a single recursive ``**`` walk.

    Threads the two safeguards a recursive filesystem walk needs through
    :func:`_walk_filtered`:

    * ``max_entries`` -- soft cap on visited entries (dirs + files). The walk
      charges each entry via :meth:`charge`; once more than ``max_entries`` are
      visited the budget is marked :attr:`exceeded` and the walk stops EARLY
      (in BOUNDED time -- root-cause guard against the "glob hangs for tens of
      minutes" failure, whose cost is the traversal, not the match count).
      Crucially the walk does NOT raise: the partial results gathered so far
      are still useful, so the caller returns them flagged as INCOMPLETE (and
      persisted via the result store) rather than discarding everything.
    * ``stop_event`` -- an optional :class:`threading.Event` an upstream cancel
      can set so the walk (running inside ``asyncio.to_thread``, hence not
      forcibly killable) breaks cooperatively via :meth:`check_stop`. Unlike
      the soft cap, an explicit cancel DOES raise :class:`ToolError` (the user
      asked to abort -- there is no partial result to hand back).
    """

    max_entries: int = field(default_factory=lambda: WALK_MAX_ENTRIES)
    stop_event: threading.Event | None = None
    _visited: int = field(default=0, init=False)
    exceeded: bool = field(default=False, init=False)

    def charge(self, n: int) -> bool:
        """Account ``n`` traversed entries; mark + return :attr:`exceeded`.

        Returns ``True`` once the cumulative visited count passes
        ``max_entries`` so :func:`_walk_filtered` can stop yielding. Does NOT
        raise -- the partial walk is returned as INCOMPLETE by the caller.
        """
        self._visited += n
        if self._visited > self.max_entries:
            self.exceeded = True
        return self.exceeded

    def check_stop(self) -> None:
        """Raise :class:`ToolError` when an upstream cancel set ``stop_event``."""
        if self.stop_event is not None and self.stop_event.is_set():
            raise ToolError("search cancelled")


def _walk_filtered(
    root: Path,
    extra_skip: frozenset[str] | None = None,
    budget: WalkBudget | None = None,
) -> Iterable[tuple[Path, list[str], list[str]]]:
    """Walk ``root`` (skipping heavyweight dirs), bounded by ``budget``.

    When ``budget`` is supplied the walk is cooperatively interruptible and
    bounded:

    * Each visited entry (directories + files) is charged against the budget.
      Once more than :data:`WALK_MAX_ENTRIES` are visited the walk yields the
      current directory (so its files still count toward the partial result)
      and then STOPS -- it does not raise. The caller inspects
      ``budget.exceeded`` and returns the partial results flagged as
      INCOMPLETE (root-cause guard against "glob hangs", in BOUNDED time,
      without discarding the useful prefix it already gathered).
    * Between directories the budget's ``stop_event`` (if set by an upstream
      cancel) is checked; when fired the walk RAISES so a user "Stop" breaks
      the traversal cooperatively (``os.walk`` in a thread is not forcibly
      killable, so the only reliable cancel is this check).

    ``budget=None`` keeps the legacy unbounded behaviour (used by callers that
    do not need the guard, e.g. small known directories).

    KNOWN LIMITATION (``os.walk`` semantics): the budget is charged per
    DIRECTORY iteration, so it bounds a WIDE tree (many directories) well, but a
    single directory containing an enormous number of files is only charged
    once -- and ``os.walk``'s internal ``scandir`` of that one directory (the
    expensive part there) runs BEFORE this charge. So a pathological
    single-directory-with-500k-files is not bounded as tightly as a wide tree.
    This is a rare shape and an inherent ``os.walk`` trait; the common
    "huge nested tree" case (the real "glob hangs" trigger) is well covered.
    """
    for dirpath_str, dirnames, filenames in os.walk(str(root)):
        dirpath = Path(dirpath_str)
        over = False
        if budget is not None:
            budget.check_stop()
            # Charge this directory + its immediate children before descending
            # so the cap reflects how many entries we have actually traversed.
            over = budget.charge(1 + len(dirnames) + len(filenames))
        if over:
            # Soft cap reached: yield THIS directory's files (they are useful
            # partial results) but do not descend further -- stop the walk so
            # it returns in bounded time. ``budget.exceeded`` tells the caller.
            yield dirpath, [], filenames
            return
        dirnames[:] = [
            d for d in dirnames if not _is_skip_dir(dirpath / d, extra_skip)
        ]
        yield dirpath, dirnames, filenames


# ---------------------------------------------------------------------------
# project_skip_dirs source (V1 parity: ``_glob._get_skip_dir_names`` merges
# the hardcoded defaults with ``_SANDBOX_CONFIG.project_skip_dirs`` from
# forge_config).  The V2 ``CodingSessionConfig`` value object carries no
# ``project_skip_dirs`` field, so the apps/api wiring root installs the
# user-configured names here via :func:`set_project_skip_dirs` at tool-bridge
# build time.  The search handlers consume it through
# :func:`get_project_skip_dirs`.  Empty by default → behaviour identical to
# the prior hardcoded-only skip set.
# ---------------------------------------------------------------------------

_PROJECT_SKIP_DIRS: list[frozenset[str]] = [frozenset()]


def set_project_skip_dirs(names: Iterable[str] | None) -> None:
    """Install the user-configured extra skip-dir names (lower-cased).

    Called by ``apps/api`` when forge_config's ``project_skip_dirs`` is
    available.  ``None`` / empty resets to the default (no extras).
    """
    if not names:
        _PROJECT_SKIP_DIRS[0] = frozenset()
        return
    _PROJECT_SKIP_DIRS[0] = frozenset(
        n.lower() for n in names if isinstance(n, str) and n
    )


def get_project_skip_dirs() -> frozenset[str]:
    """Return the merged extra skip-dir names (defaults are applied separately
    inside :func:`_is_skip_dir`)."""
    return _PROJECT_SKIP_DIRS[0]


# ---------------------------------------------------------------------------
# Tool-result-store retrieval roots (退化 #11 / subtask 2).
#
# The oversized-output store (``tool_result_store.FileSystemToolResultStore``)
# persists exec stdout/stderr and large grep/glob results under the
# application's own ``data/tool_results/`` directory, then tells the model to
# ``read(path=<root>/<file>)`` to recover the full body.  When the operator
# leaves the FileGuard master switch OFF (V1 default) every read passes
# through, so retrieval works.  But when the operator turns FileGuard ON
# WITHOUT allow-listing the application data dir, a ``read`` of the persisted
# file is an implicit-deny path miss (``CheckPermissionUseCase`` → DENY) and
# the model can never recover the elided middle — the very dependency the
# store docstring warns about.
#
# To make the "saved file is always retrievable" guarantee explicit and
# independent of the operator's policy config, the apps/api wiring root
# registers the store root(s) here at tool-bridge build time.  ``tool_read``
# treats a path resolved UNDER one of these roots as system-owned retrieval
# (the file was written by this process for the model to read back) and
# SKIPS the FileGuard read gate for it — exactly the V1 ``get_stored_result``
# behaviour, which read STORAGE_DIR files directly without going through the
# FileGuard allowlist.  The set is empty by default (no roots trusted →
# behaviour identical to before), so test / minimal wirings are unaffected.
# ---------------------------------------------------------------------------

_TOOL_RESULT_STORE_ROOTS: list[tuple[Path, ...]] = [()]


def set_tool_result_store_roots(roots: Iterable[str | Path] | None) -> None:
    """Install the resolved tool-result-store root(s) trusted for ``read``.

    Called by ``apps/api`` with the :class:`FileSystemToolResultStore` root
    so the persisted oversized-output files are always retrievable via the
    ``read`` tool regardless of the FileGuard policy.  ``None`` / empty
    resets to "no trusted roots" (the default).  Each root is resolved once
    here so the per-read check is a cheap prefix compare.
    """
    if not roots:
        _TOOL_RESULT_STORE_ROOTS[0] = ()
        return
    resolved: list[Path] = []
    for r in roots:
        try:
            resolved.append(Path(r).resolve())
        except (ValueError, OSError):  # pragma: no cover — defensive
            continue
    _TOOL_RESULT_STORE_ROOTS[0] = tuple(resolved)


def is_under_tool_result_store_root(path_str: str) -> bool:
    """Return ``True`` iff ``path_str`` resolves under a trusted store root.

    Used by :func:`tool_read` to bypass the FileGuard read gate for files
    the store itself persisted (V1 ``get_stored_result`` read STORAGE_DIR
    directly).  Resolution failures fall back to a literal compare so a
    not-yet-created path under the root still matches.  Empty trusted-root
    set → always ``False`` (no bypass).
    """
    roots = _TOOL_RESULT_STORE_ROOTS[0]
    if not roots or not path_str:
        return False
    try:
        target = Path(path_str).resolve()
    except (ValueError, OSError):  # pragma: no cover — defensive
        target = Path(path_str)
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Default workspace base for relative path / cwd resolution.
#
# Historically the tool handlers resolved relative paths and a missing exec
# ``cwd`` against ``Path.cwd()`` == the daemon process CWD == the repo root
# (the application install dir). That leaked the application's own source
# tree to the agent and let model-builder runs pollute the repo root with
# stray artifacts (``null/`` / ``tmp_<pid>/`` / ``*.bat``).
#
# Instead, the chat agentic loop sets a per-request *workspace base*
# (the active session's workspace, falling back to the global configured
# workspace, default ``C:/WoS_AI``) so EVERY file/exec tool in that request
# shares the same working root — the "workspace" concept is session-/
# app-wide, not per-tool. A :class:`contextvars.ContextVar` keeps this
# concurrency-safe across interleaved async requests (a process-global
# ``os.chdir`` would race between sessions).
#
# When unset (e.g. CC/OC code mode, or callers that pass an explicit
# absolute path / ``cwd``) resolution falls back to the prior behaviour so
# nothing regresses.
# ---------------------------------------------------------------------------

_workspace_base_var: ContextVar[str | None] = ContextVar(
    "qai_tool_workspace_base", default=None
)


def set_workspace_base(base: str | None) -> object:
    """Bind the per-request default workspace base; returns a reset token.

    Pass the token to :func:`reset_workspace_base` (or use
    ``_workspace_base_var.reset(token)``) once the request completes. A
    blank / ``None`` value clears the binding (fall back to ``Path.cwd()``).
    """
    cleaned = (base or "").strip() or None
    return _workspace_base_var.set(cleaned)


def reset_workspace_base(token: object) -> None:
    """Restore the workspace base to its previous value."""
    try:
        _workspace_base_var.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):  # pragma: no cover — defensive
        pass


def get_workspace_base() -> str | None:
    """Return the current per-request workspace base, or ``None``."""
    return _workspace_base_var.get()


# ---------------------------------------------------------------------------
# Per-request APP_ROOT (install/repo root) for SKILL.md placeholder expansion
# ---------------------------------------------------------------------------
# SKILL.md files reference bundled scripts / config / SDK assets via the
# ``${APP_ROOT}`` placeholder (the install/repo root — see
# ``apps.api._chat_feature_skill_provider.APP_ROOT_PLACEHOLDER``). When a SKILL
# is injected into the system prompt the provider substitutes it there; but a
# SKILL loaded on demand — via the ``read`` tool (reading ``SKILL.md``) or the
# ``skill`` tool — does NOT go through that path, so the placeholder would leak
# to the model as a literal and the agent would build a broken path (the file
# tools' relative-path base is the WORKSPACE, not the repo root).
#
# We thread the real APP_ROOT the same way as the workspace base: a ContextVar
# bound for the duration of each request at the DI ToolPort boundary, read when
# a SKILL.md's body is returned so ``${APP_ROOT}`` becomes the real absolute
# install path (correct in BOTH dev and packaged/release, since the value comes
# from ``container.repo_root``). ``None`` means "no APP_ROOT context" (e.g. a
# bare unit test), in which case the placeholder is left verbatim (fail-safe —
# never crash, never fabricate a path).
# ---------------------------------------------------------------------------
# APP_ROOT / SKILL_DIR placeholder expansion (BC-shared).
#
# The ContextVar and the four helpers below are the *single* live definition
# in the process — they live in ``qai.platform.skills.placeholders`` (shared
# kernel) so both the ``ai_coding`` tool handlers and the ``chat`` skill
# loader read the same binding without crossing a Bounded-Context boundary
# (``.importlinter`` contract 3). We re-export them from here to keep this
# module's long-standing public surface stable — ``apps.api.di``, the
# ``handlers`` package ``__init__``, ``read_write.py``, and unit tests all
# still import ``set_app_root`` / ``get_app_root`` / ``reset_app_root`` /
# ``expand_skill_placeholders`` from this module.
# ---------------------------------------------------------------------------
from qai.platform.skills.placeholders import (
    _app_root_var,
    expand_skill_placeholders,
    get_app_root,
    reset_app_root,
    set_app_root,
)


# ---------------------------------------------------------------------------
# Per-request conversation scope (SEC — true session-scoped grants)
# ---------------------------------------------------------------------------
# The security layer needs the TOP-LEVEL conversation id at the point a file
# tool calls ``file_guard.enforce_*`` so a ``session``-scoped grant can be
# matched only within its own collaboration session (main agent + all
# sub-agents / participants share the same top-level conversation id). We
# thread it the same way as the workspace base: a ContextVar bound for the
# duration of each ``ToolInvocationRequest`` at the DI ToolPort boundary, read
# by the FileGuard bridge. ``None`` means "no conversation context" (e.g. a
# bare unit test), in which case only permanent grants apply (fail-safe).
_conversation_scope_var: ContextVar[str | None] = ContextVar(
    "qai_tool_conversation_scope", default=None
)


def set_conversation_scope(conversation_id: str | None) -> object:
    """Bind the per-request top-level conversation id; returns a reset token.

    Pass the token to :func:`reset_conversation_scope` once the request
    completes. A blank / ``None`` value clears the binding.
    """
    cleaned = (conversation_id or "").strip() or None
    return _conversation_scope_var.set(cleaned)


def reset_conversation_scope(token: object) -> None:
    """Restore the conversation scope to its previous value."""
    try:
        _conversation_scope_var.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):  # pragma: no cover — defensive
        pass


def get_conversation_scope() -> str | None:
    """Return the current per-request top-level conversation id, or ``None``."""
    return _conversation_scope_var.get()


def resolve_under_workspace(path_str: str) -> str:
    """Resolve a (possibly relative) path against the workspace base.

    Absolute paths are returned unchanged. Relative paths are joined onto
    the per-request workspace base when one is set; otherwise they are
    returned unchanged (legacy ``Path.cwd()`` resolution by the caller).
    """
    if not path_str:
        return path_str
    base = _workspace_base_var.get()
    if not base:
        return path_str
    try:
        if Path(path_str).is_absolute():
            return path_str
        return str(Path(base) / path_str)
    except (ValueError, OSError):  # pragma: no cover — defensive
        return path_str


def default_cwd() -> str | None:
    """Return the workspace base to use as a default exec/glob CWD.

    ``None`` means "no override" — the caller keeps its legacy behaviour
    (inherit the process CWD).
    """
    return _workspace_base_var.get()
