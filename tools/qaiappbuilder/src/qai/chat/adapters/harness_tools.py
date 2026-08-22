# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat-side "agent harness" tool handlers — ``todowrite`` and ``question``.

These two tools are V2 enhancements (V1 has no equivalent) registered directly
on the chat-side :class:`~qai.chat.adapters.tool_invocation.RegistryBackedToolInvocation`
registry, exactly like the ``agent`` sub-agent tool
(``apps/api/_chat_di.py``).  They are deliberately NOT added to the
``qai.ai_coding`` ``TOOL_SCHEMAS`` file-tool family: those are
filesystem/search/exec operations, whereas these are *harness control* tools
that drive UI surfaces (a live task-list panel / a blocking question dialog).
Keeping them in the chat context preserves single responsibility and avoids
churning the locked ai_coding 9-tool set.

Both handlers have the standard chat tool signature
``async (ToolInvocationRequest) -> Any``; the returned value becomes the
TOOL_RESULT fed back to the model.

``todowrite``
    One-shot: validates + echoes the todo list back to the model.  The list is
    carried verbatim on the ``tool_call`` frame's ``arguments`` so the frontend
    renders the live task-list panel from it; the handler's return value is a
    concise text confirmation the model reads to know the write succeeded.

``question``
    Blocking: registers a pending answer future in the injected
    :class:`InMemoryQuestionRegistry`, then ``await``\\s it (raced against the
    stream's abort handle + a timeout) until the user submits an answer via the
    answer endpoint.  The ``question_id`` + question text + options travel on
    the ``tool_call`` frame's ``arguments`` so the frontend can pop the dialog
    and POST the answer back.

    The schema accepts EITHER a single question (legacy ``question`` /
    ``header`` / ``options`` / ``multiple`` top-level fields, kept for backward
    compatibility) OR a top-level ``questions`` array carrying 1..N related
    questions at once.  The frontend renders the array as a paginated
    question-card (one page per question) with review + a single batched
    submit.  Regardless of how many questions are asked, the handler still
    blocks on exactly ONE answer future per ``tab_id`` and the user replies with
    a single ``answer`` string (the frontend stitches the per-question answers
    into that one string; the backend does not parse its internal structure).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from qai.chat.adapters.question_registry import InMemoryQuestionRegistry
from qai.chat.application.ports import (
    ConversationRepositoryPort,
    StreamAbortRegistryPort,
    SubAgentSessionRepositoryPort,
    ToolInvocationRequest,
)
from qai.chat.application.sub_agent_stream_broadcaster import (
    SubAgentStreamBroadcaster,
)
from qai.chat.application.use_cases.implementation_plan import (
    FeatureItem,
    ImplementationPlan,
    read_implementation_plan,
    write_implementation_plan,
)
from qai.chat.domain.ids import ConversationId, SubAgentSessionId
from qai.chat.domain.sub_agent_session import (
    SubAgentSession,
    SubAgentSessionStatus,
)
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock, SystemClock

__all__ = [
    "TodoWriteToolHandler",
    "QuestionToolHandler",
    "SkillToolHandler",
    "ImplementationPlanToolHandler",
    "TODOWRITE_TOOL_SCHEMA",
    "QUESTION_TOOL_SCHEMA",
    "SKILL_TOOL_SCHEMA",
    "IMPLEMENTATION_PLAN_TOOL_SCHEMA",
]

_log = get_logger(__name__)

# Default ceiling for how long a ``question`` blocks waiting for the user.
#
# ``0`` / ``None`` (the default; operator may override via
# ``ChatSettings.question_timeout_seconds``) means "wait until the user answers
# or the stream is aborted" — NO auto-cancel. A forgotten dialog is never
# auto-expired out from under the user; the abort registry / ``CancelledError``
# path still lets a "stop" dismiss it cleanly, so the connection is never
# *truly* unkillable.
#
# A positive override installs a hard cap so a forgotten dialog auto-expires
# (e.g. to stop it pinning an SSE/WS connection open — State-Truth-First §铁律5).
_DEFAULT_QUESTION_TIMEOUT_SECONDS = 0.0


# ---------------------------------------------------------------------------
# Schemas (OpenAI function-calling shape; identical convention to
# ``qai.ai_coding`` TOOL_SCHEMAS so the wire ``payload["tools"]`` element is
# uniform across every tool surface).
# ---------------------------------------------------------------------------

TODOWRITE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todowrite",
        "description": (
            "Create and manage a structured task list for the current "
            "session. Call this to plan a multi-step task, track progress, "
            "and show the user what you are doing. Pass the COMPLETE list "
            "every time (it replaces the previous list, it is not a delta). "
            "Mark exactly one task 'in_progress' at a time and flip a task to "
            "'completed' as soon as it is done. Use it for non-trivial work "
            "of 3+ steps; skip it for a single trivial task."
        ),
        "parameters": {
            "type": "object",
            "required": ["todos"],
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full, updated task list.",
                    "items": {
                        "type": "object",
                        "required": ["content", "status"],
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Short description of the task.",
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "cancelled",
                                ],
                                "description": "Current state of the task.",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Optional priority level.",
                            },
                        },
                    },
                },
            },
        },
    },
}

# Per-question option shape, shared by the single-question fields and each
# element of the ``questions`` array so the wire schema stays consistent.
_QUESTION_OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Optional list of suggested choices.",
    "items": {
        "type": "object",
        "required": ["label"],
        "properties": {
            "label": {
                "type": "string",
                "description": "Display text for the choice.",
            },
            "description": {
                "type": "string",
                "description": "Optional explanation of choice.",
            },
        },
    },
}

QUESTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "question",
        "description": (
            "Ask the user one or more questions and BLOCK until they answer. "
            "Use it to clarify ambiguous requirements or get a decision before "
            "continuing. PREFER the 'questions' array: pass 1..N related "
            "questions at once, each with a clear prompt and 2-5 options (the "
            "user may also type a custom answer); you get every answer back in "
            "one shot. The single 'question' field is a legacy one-off form. Do "
            "NOT use this for anything you can find out yourself with other "
            "tools. Returns the user's answer(s)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": (
                        "Preferred: 1..N related questions asked at once "
                        "(paginated, reviewable card)."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["question"],
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question to ask the user.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Optional short label (<=30 chars).",
                            },
                            "options": _QUESTION_OPTIONS_SCHEMA,
                            "multiple": {
                                "type": "boolean",
                                "description": (
                                    "Allow selecting more than one option."
                                ),
                            },
                        },
                    },
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Single-question form (legacy). Prefer 'questions'."
                    ),
                },
                "header": {
                    "type": "string",
                    "description": "Optional short label (<=30 chars).",
                },
                "options": _QUESTION_OPTIONS_SCHEMA,
                "multiple": {
                    "type": "boolean",
                    "description": "Allow selecting more than one option.",
                },
            },
        },
    },
}

SKILL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Load a specialized skill: a focused, expert instruction set for a "
            "specific kind of task. Call this when the task at hand matches one "
            "of the skills listed under 'available_skills' (or your skills "
            "context) — it returns that skill's instructions so you can follow "
            "its workflow. The 'name' argument MUST be the exact id of a skill "
            "from available_skills; do NOT invent a name. This is a read-only "
            "operation: it only injects the skill's written guidance into the "
            "conversation and NEVER executes any code from the skill.\n"
            "The skill's instructions are read as numbered lines, paginated "
            "like the `read` tool: a single call returns at most a bounded "
            "window (about 250 lines / 10000 characters). If the skill is "
            "longer, the result footer reports the shown line range, the total "
            "line count, and the exact `offset` for the next page — call `skill` "
            "AGAIN with the same `name` and that `offset` to continue reading "
            "the remaining lines. Repeat until you have read the lines you need."
        ),
        "parameters": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The exact id of the skill to load; must come from "
                        "available_skills."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "1-based line number to start reading from. Defaults to "
                        "1 (start of the skill). Use the `offset` reported in a "
                        "previous truncation footer to read the next page."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of lines to return in this call. "
                        "Defaults to the built-in page size (250 lines); values "
                        "above it are clamped. Output is also capped at ~10000 "
                        "characters regardless of line count."
                    ),
                },
            },
        },
    },
}


IMPLEMENTATION_PLAN_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "implementation_plan",
        "description": (
            "Register a LIGHTWEIGHT structured implementation checklist for "
            "this multi-agent discussion (coordinator/主持人 only). Call this "
            "AFTER you have written the full plan document to the workspace "
            "file (实施计划.md) with the write tool. Each item is a short "
            "pointer, NOT a rewrite of the plan detail: give a concise "
            "'title', auto-assign the most suitable participant to "
            "'assigned_role', and OPTIONALLY set 'depends_on' (titles/ids of "
            "prerequisite items), a 'verify_command' (a short shell command "
            "whose exit code objectively judges the item done), a 'doc_ref' "
            "(the plan-document section this item maps to, e.g. a heading), "
            "and 'acceptance_criteria'. The executing role reads 实施计划.md "
            "for the full requirements — do NOT paste the detail here. Pass "
            "the COMPLETE item list every call (it replaces the previous "
            "registration)."
        ),
        "parameters": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "description": "The full, ordered feature-item checklist.",
                    "items": {
                        "type": "object",
                        "required": ["title"],
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": (
                                    "Short human label for the work item."
                                ),
                            },
                            "assigned_role": {
                                "type": "string",
                                "description": (
                                    "Participant id of the role auto-assigned "
                                    "to implement this item."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Titles or ids of items that must complete "
                                    "before this one."
                                ),
                            },
                            "verify_command": {
                                "type": "string",
                                "description": (
                                    "Short shell command whose exit code judges "
                                    "this item done (e.g. a focused test run)."
                                ),
                            },
                            "doc_ref": {
                                "type": "string",
                                "description": (
                                    "The section of 实施计划.md this item maps "
                                    "to (e.g. a heading), for the executor to "
                                    "read the full detail."
                                ),
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional short 'done when…' bullet checks."
                                ),
                            },
                        },
                    },
                },
                "plan_doc_path": {
                    "type": "string",
                    "description": (
                        "Workspace path of the full plan document (defaults to "
                        "实施计划.md)."
                    ),
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# todowrite
# ---------------------------------------------------------------------------

_VALID_TODO_STATUS = frozenset(
    {"pending", "in_progress", "completed", "cancelled"}
)


class TodoWriteToolHandler:
    """One-shot handler for the ``todowrite`` tool.

    Validates the ``todos`` payload and returns a concise text confirmation.
    The actual rendering of the task-list panel is driven by the frontend off
    the ``tool_call`` frame arguments — this handler exists so the model gets a
    deterministic success/error result and so the list is sanity-checked.
    """

    __slots__ = ()

    async def execute(self, request: ToolInvocationRequest) -> str:
        todos = request.arguments.get("todos")
        if not isinstance(todos, list) or not todos:
            return (
                "[todowrite error] 'todos' must be a non-empty array of "
                "{content, status} objects."
            )
        counts = {"pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
        normalized: list[str] = []
        for index, item in enumerate(todos):
            if not isinstance(item, dict):
                return (
                    f"[todowrite error] todos[{index}] must be an object with "
                    "'content' and 'status'."
                )
            content = item.get("content")
            status = item.get("status")
            if not isinstance(content, str) or not content.strip():
                return (
                    f"[todowrite error] todos[{index}].content must be a "
                    "non-empty string."
                )
            if status not in _VALID_TODO_STATUS:
                return (
                    f"[todowrite error] todos[{index}].status must be one of "
                    f"{sorted(_VALID_TODO_STATUS)}."
                )
            counts[status] += 1
            marker = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
                "cancelled": "[-]",
            }[status]
            normalized.append(f"{marker} {content.strip()}")

        if counts["in_progress"] > 1:
            # Soft warning only — the model's own discipline says "one at a
            # time"; we surface it but do not reject so a re-plan still works.
            _log.info(
                "chat.todowrite.multiple_in_progress",
                tab_id=str(request.tab_id),
                in_progress=counts["in_progress"],
            )

        summary = (
            f"Task list updated ({len(todos)} task(s): "
            f"{counts['completed']} completed, "
            f"{counts['in_progress']} in progress, "
            f"{counts['pending']} pending"
            + (
                f", {counts['cancelled']} cancelled"
                if counts["cancelled"]
                else ""
            )
            + ")."
        )
        return summary + "\n" + "\n".join(normalized)



# ---------------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------------

# Per-call ceilings on the SKILL.md text injected back to the model.
#
# A SKILL.md is injected verbatim into the model's context, so one oversized
# skill could dominate the context window in a single call. Instead of a single
# large hard cut (the old 50_000-char tail truncation, which dropped the
# remainder with NO recovery path since the tool took only ``name``), the skill
# tool now paginates by LINE exactly like the ``read`` tool: each call returns a
# bounded window and, when the skill is longer, a footer reports the shown line
# range + total lines + the ``offset`` for the next page so the model can call
# ``skill`` again to read the remainder. A long, legitimate skill (e.g. a ~70KB
# model-builder SKILL.md) is therefore fully reachable across several small
# pages rather than truncated once.
#
# Two independent per-call bounds (whichever is hit first ends the page):
#   * ``_SKILL_MAX_LINES`` — line-count window (the primary, model-friendly
#     bound; maps directly to the ``offset`` continuation contract).
#   * ``_SKILL_CONTENT_MAX_CHARS`` — a character backstop so a page of very long
#     lines still cannot blow the budget (~10K chars ≈ 2.5-3K tokens).
_SKILL_MAX_LINES = 250
_SKILL_CONTENT_MAX_CHARS = 10_000

# Lookup type: a zero-arg callable returning a mapping of ``skill_id`` ->
# absolute path to that skill's SKILL.md file. Injected at DI time so this
# adapter never imports ``qai.platform.skills`` (or any discovery machinery)
# directly — keeping the handler trivially testable with a fake mapping and
# decoupling "how skills are discovered/located" from "how the tool runs".
SkillLookup = Callable[[], "Mapping[str, str]"]


class SkillToolHandler:
    """Handler for the ``skill`` tool: load one skill's SKILL.md text.

    The tool lets the model pull in a focused, expert instruction set on
    demand instead of carrying every skill's full prose in the system prompt.
    It is strictly READ-ONLY: it resolves the requested skill name against the
    currently-visible catalog, reads that skill's SKILL.md as UTF-8 text, and
    returns it wrapped in a ``<skill_content name="...">...</skill_content>``
    envelope the model can consume. It NEVER executes any code from the skill.

    Decoupling (``context-isolation`` friendliness + testability): the handler
    does not import ``qai.platform.skills``. Instead it receives ``skill_lookup``
    — a zero-arg callable returning a ``{skill_id: skill_md_path}`` mapping —
    injected by ``apps/api`` at DI time. Tests pass a plain ``lambda`` returning
    a dict pointing at a temp file. ``skill_lookup`` raising / returning empty
    degrades to a stable "no skills available" / "unknown skill" result rather
    than raising out of the handler.
    """

    __slots__ = ("_skill_lookup", "_max_chars", "_max_lines")

    def __init__(
        self,
        *,
        skill_lookup: SkillLookup,
        max_chars: int = _SKILL_CONTENT_MAX_CHARS,
        max_lines: int = _SKILL_MAX_LINES,
    ) -> None:
        self._skill_lookup = skill_lookup
        self._max_chars = max_chars
        self._max_lines = max_lines

    async def execute(self, request: ToolInvocationRequest) -> str:
        name = request.arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            return (
                "[skill error] provide a non-empty 'name' string naming a "
                "skill from available_skills."
            )
        name = name.strip()

        try:
            catalog = dict(self._skill_lookup() or {})
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.skill.lookup_failed",
                skill_name=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                "[skill error] no skills are available right now; proceed "
                "without a specialized skill."
            )

        if not catalog:
            return (
                "[skill error] no skills are available; proceed without a "
                "specialized skill."
            )

        skill_path = catalog.get(name)
        if not skill_path:
            available = ", ".join(sorted(catalog)) or "(none)"
            return (
                f"[skill error] unknown skill {name!r}. Available skills: "
                f"{available}."
            )

        try:
            content = self._read_skill_text(skill_path)
        except Exception as exc:  # noqa: BLE001 — never break the turn
            # Note: do NOT leak the on-disk path or a stack trace to the model.
            _log.warning(
                "chat.skill.read_failed",
                skill_name=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                f"[skill error] could not load skill {name!r}; proceed "
                "without it."
            )

        if not content.strip():
            return (
                f"[skill error] skill {name!r} has no instructions; proceed "
                "without it."
            )

        # Line-based pagination (parity with the ``read`` tool): resolve the
        # requested window, slice by LINE, and clamp by a character backstop.
        offset = self._coerce_positive_int(request.arguments.get("offset"), 1)
        limit = self._coerce_positive_int(
            request.arguments.get("limit"), self._max_lines
        )
        if limit > self._max_lines:
            limit = self._max_lines

        page_body, footer = self._paginate(
            content, offset=offset, limit=limit
        )

        # Escape the name in the XML-ish attribute defensively. ``name`` is
        # already a validated catalog key (a SKILL.md parent-dir basename, so in
        # practice plain ``[\w-]``), but escaping keeps the envelope well-formed
        # even if a future skill id carries ``"`` / ``<`` / ``&`` — consistent
        # with the ``html.escape`` used for the ``<available_skills>`` XML.
        import html as _html

        safe_name = _html.escape(name, quote=True)
        envelope = (
            f'<skill_content name="{safe_name}">\n{page_body}\n</skill_content>'
        )
        if footer:
            envelope += footer
        return envelope

    @staticmethod
    def _coerce_positive_int(raw: Any, default: int) -> int:
        """Coerce a tool argument into a positive int, falling back to default.

        The model may send ``offset`` / ``limit`` as an int, a numeric string,
        or omit it entirely / send junk. Any non-positive or unparseable value
        degrades to ``default`` so pagination never raises out of the handler.
        """
        if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
            return default
        if isinstance(raw, int):
            return raw if raw > 0 else default
        if isinstance(raw, str):
            try:
                value = int(raw.strip())
            except (ValueError, TypeError):
                return default
            return value if value > 0 else default
        return default

    def _paginate(
        self, content: str, *, offset: int, limit: int
    ) -> tuple[str, str]:
        """Return ``(page_body, footer)`` for a 1-based line window.

        The body is the ``[offset, offset+limit)`` line slice, further clamped
        so it never exceeds ``self._max_chars`` (a long-line backstop). The
        footer ALWAYS reports the total line count and the shown line range; when
        more lines remain it also reports the ``offset`` for the next page so the
        model can call ``skill`` again to read the remainder (parity with the
        ``read`` tool's ``offset=N`` continuation contract). Surfacing the total
        on every call means the model always knows the skill's full size.
        """
        # ``splitlines`` drops the trailing newline semantics we don't need for
        # display; we rejoin with ``\n`` so numbered continuation stays stable.
        lines = content.splitlines()
        total_lines = len(lines)

        start_idx = offset - 1  # offset is 1-based
        if start_idx >= total_lines:
            # Past the end — nothing to show; tell the model where the end is.
            return (
                "",
                (
                    f"\n[skill note] offset={offset} is past the end of the "
                    f"skill (total {total_lines} line(s)); nothing to read."
                ),
            )

        end_idx = min(start_idx + limit, total_lines)
        window = lines[start_idx:end_idx]
        page_body = "\n".join(window)

        # Character backstop: if the line window is still too large (very long
        # lines), keep only the WHOLE lines that fit under the char cap and
        # recompute ``end_idx`` so the continuation ``offset`` stays
        # line-accurate. ``line_hard_cut`` records the special case where even a
        # SINGLE line exceeds the cap (that one line is hard-cut mid-line, so its
        # remainder is NOT recoverable by a line ``offset`` — we say so).
        char_truncated = False
        line_hard_cut = False
        if len(page_body) > self._max_chars:
            char_truncated = True
            kept_lines: list[str] = []
            running = 0
            for line in window:
                # +1 for the ``\n`` join between lines (not before the first).
                added = len(line) + (1 if kept_lines else 0)
                if running + added > self._max_chars:
                    break
                kept_lines.append(line)
                running += added
            if not kept_lines:
                # Even the FIRST line alone exceeds the char cap — hard-cut that
                # single line mid-content (the only way to honour the cap).
                line_hard_cut = True
                kept_lines = [window[0][: self._max_chars]]
                end_idx = start_idx + 1
            else:
                end_idx = start_idx + len(kept_lines)
            page_body = "\n".join(kept_lines)

        shown_start = start_idx + 1
        shown_end = end_idx
        remaining = total_lines - shown_end

        if remaining <= 0 and not char_truncated:
            # Whole skill (or its tail) fit — no continuation needed, but still
            # report the shown range + total line count so the model always
            # knows the skill's size (e.g. to decide whether an earlier page it
            # skipped is worth reading) — parity with always surfacing total.
            footer = (
                f"\n[skill note] showed lines {shown_start}-{shown_end} of "
                f"total {total_lines}; end of skill reached."
            )
            return page_body, footer

        if line_hard_cut and remaining <= 0:
            # A single over-long FINAL line was hard-cut mid-content: there are
            # no further LINES to read (a line ``offset`` would land past the
            # end), and the cut line's tail is not line-recoverable. Be explicit
            # rather than pointing at an empty next page.
            footer = (
                f"\n[skill note] showed lines {shown_start}-{shown_end} of "
                f"total {total_lines}; line {shown_end} exceeded the "
                f"{self._max_chars}-character page cap and was cut mid-line — "
                f"its remainder is not separately readable. End of skill "
                f"reached."
            )
            return page_body, footer

        next_offset = shown_end + 1
        if line_hard_cut:
            # A single over-long line was hard-cut but MORE whole lines remain
            # after it; continue from the next line (the cut line's tail is not
            # separately recoverable).
            reason = (
                f"line {shown_end} exceeded the {self._max_chars}-character "
                f"page cap and was cut mid-line"
            )
        else:
            reason = (
                f"page capped at {self._max_lines} lines / "
                f"{self._max_chars} characters"
            )
        footer = (
            f"\n[skill note] showed lines {shown_start}-{shown_end} of total "
            f"{total_lines} ({reason}); {max(0, remaining)} more line(s) "
            f"available — call `skill` again with the same name and "
            f"offset={next_offset} to continue reading."
        )
        return page_body, footer

    @staticmethod
    def _read_skill_text(skill_path: str) -> str:
        """Read a SKILL.md file as UTF-8 text (strict), expanding placeholders.

        Isolated so tests can stub it and so the UTF-8 contract (§3.10) is in
        one place. A decoding error / missing file surfaces as the generic
        ``[skill error]`` result via ``execute``'s ``except`` (no path leak).

        The ``skill`` tool loads a SKILL on demand (not via the system-prompt
        injection path), so it must expand the SKILL.md path placeholders itself
        — ``${APP_ROOT}`` → install/repo root, ``${SKILL_DIR}`` → the skill's own
        dir — the same way the ``read`` tool does when returning a SKILL.md body.
        Otherwise a placeholder would leak to the model as a literal and the
        agent would build a broken path. Unavailable placeholders are left
        verbatim (fail-safe). The expansion helper lives in the platform
        shared kernel (``qai.platform.skills.placeholders``) so both the chat
        skill loader and the ``ai_coding`` tool handlers read the same
        ``APP_ROOT`` binding without crossing a Bounded-Context boundary.
        """
        from pathlib import Path

        text = Path(skill_path).read_text(encoding="utf-8")
        if "${" not in text:
            return text
        try:
            from qai.platform.skills.placeholders import (
                expand_skill_placeholders,
            )
        except Exception:  # noqa: BLE001 — never break skill loading
            return text
        return expand_skill_placeholders(
            text, skill_dir=str(Path(skill_path).parent)
        )


# ---------------------------------------------------------------------------
# question
# ---------------------------------------------------------------------------


def _parse_one_question(raw: Any) -> dict[str, Any] | None:
    """Normalize a single question object, or ``None`` if it is invalid.

    Shared by both the single-question (legacy top-level fields) and the
    multi-question (``questions`` array) forms so option / ``multiple``
    validation stays identical across them.  Returns a sanitized dict with a
    guaranteed non-empty ``question`` string; ``options`` / ``header`` /
    ``multiple`` are only included when well-formed.
    """
    if not isinstance(raw, dict):
        return None
    question_text = raw.get("question")
    if not isinstance(question_text, str) or not question_text.strip():
        return None

    parsed: dict[str, Any] = {"question": question_text.strip()}

    header = raw.get("header")
    if isinstance(header, str) and header.strip():
        parsed["header"] = header.strip()

    multiple = raw.get("multiple")
    if isinstance(multiple, bool):
        parsed["multiple"] = multiple

    options = raw.get("options")
    if isinstance(options, list):
        normalized_options: list[dict[str, str]] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            label = opt.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            entry: dict[str, str] = {"label": label.strip()}
            desc = opt.get("description")
            if isinstance(desc, str) and desc.strip():
                entry["description"] = desc.strip()
            normalized_options.append(entry)
        if normalized_options:
            parsed["options"] = normalized_options

    return parsed


def _parse_questions(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the tool arguments into a list of 1..N normalized questions.

    Resolution order (only one form ever produces questions):

    1. A non-empty top-level ``questions`` array — each valid element is
       normalized via :func:`_parse_one_question`; invalid elements are
       dropped.
    2. Otherwise the legacy single-question top-level fields
       (``question`` / ``header`` / ``options`` / ``multiple``), wrapped into a
       1-element list.

    Returns an empty list when neither form yields a valid question (the caller
    surfaces the stable ``[question error]`` result).
    """
    questions = arguments.get("questions")
    if isinstance(questions, list) and questions:
        parsed = [
            q for q in (_parse_one_question(item) for item in questions) if q
        ]
        if parsed:
            return parsed
        return []

    single = _parse_one_question(arguments)
    return [single] if single else []


class QuestionToolHandler:
    """Blocking handler for the ``question`` tool.

    Construction dependencies (chat-context-owned, so ``context-isolation`` is
    preserved):

    * ``registry`` — the :class:`InMemoryQuestionRegistry` shared with the
      answer endpoint.
    * ``abort_registry`` — used to detect a cooperative-cancellation request
      (user pressed stop) while waiting so the question does not block forever.
    * ``timeout_seconds`` — hard cap; when reached the tool returns a stable
      timeout result rather than wedging the generator open.

    Correlation with the frontend dialog is by ``tab_id``: the streaming use
    case already emits the ``tool_call`` frame (``tool_name == "question"``,
    ``arguments`` = the model's question + options) before this handler runs,
    so the UI pops the dialog off that frame and POSTs the answer back keyed by
    the same ``tab_id`` it holds — no server-minted id needs to round-trip
    through the (locked) frame schema.
    """

    __slots__ = (
        "_registry",
        "_abort_registry",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        registry: InMemoryQuestionRegistry,
        abort_registry: StreamAbortRegistryPort | None = None,
        timeout_seconds: float | None = _DEFAULT_QUESTION_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._abort_registry = abort_registry
        # Resolve the effective hard cap:
        #   * ``None``  → caller did not configure it → built-in default
        #     (``_DEFAULT_QUESTION_TIMEOUT_SECONDS``; ``0`` by default → no cap).
        #   * ``<= 0``  → explicit operator opt-out → NO cap (store ``None`` so
        #     the wait path skips ``asyncio.wait_for``'s timeout; a "stop"/abort
        #     still resolves it via the ``CancelledError`` path).
        #   * ``> 0``   → use that many seconds.
        effective = (
            _DEFAULT_QUESTION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        resolved: float | None = effective if effective > 0 else None
        self._timeout_seconds: float | None = resolved

    async def execute(self, request: ToolInvocationRequest) -> str:
        questions = _parse_questions(request.arguments)
        if not questions:
            return (
                "[question error] provide either a non-empty 'question' "
                "string or a 'questions' array with at least one valid "
                "{question} object."
            )

        tab_id = request.tab_id
        try:
            future = self._registry.create(tab_id)
        except ValueError as exc:
            # Defensive: a tab should never have two pending questions (the
            # generator is suspended on the first), but never raise out of a
            # tool handler — surface a stable result the model can read.
            return f"[question error] {exc}"

        _log.info(
            "chat.question.wait.start",
            tab_id=str(tab_id),
            question_count=len(questions),
            timeout_seconds=self._timeout_seconds,
        )

        try:
            # ``timeout=None`` makes ``asyncio.wait_for`` wait indefinitely (no
            # auto-cancel) — the ``TimeoutError`` branch then never fires; a
            # "stop"/abort still resolves via the ``CancelledError`` path.
            answer = await asyncio.wait_for(
                self._wait_for_answer(future, request),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            self._registry.cancel(tab_id)
            _log.info("chat.question.wait.timeout", tab_id=str(tab_id))
            return (
                "[question timeout] The user did not answer in time. "
                "Proceed using your best judgement or ask again if needed."
            )
        except asyncio.CancelledError:
            # Stream aborted / question cancelled out-of-band.
            self._registry.cancel(tab_id)
            _log.info("chat.question.wait.cancelled", tab_id=str(tab_id))
            return (
                "[question cancelled] The question was dismissed before the "
                "user answered."
            )

        _log.info("chat.question.wait.answered", tab_id=str(tab_id))
        return f"The user answered: {answer}"

    async def _wait_for_answer(
        self,
        future: asyncio.Future[str],
        request: ToolInvocationRequest,
    ) -> str:
        """Await ``future`` while also honouring a stream abort signal.

        If an abort handle is registered for this tab and gets signalled
        (user pressed stop) — or the handle is gone entirely (stream
        unwound) — we cancel the wait so the outer ``execute`` surfaces a
        cancelled result instead of blocking forever.  Polling the abort flag
        on a 0.25s cadence mirrors how the streaming loop already observes
        cooperative cancellation, without busy-spinning.
        """
        abort_registry = self._abort_registry
        if abort_registry is None:
            return await future
        tab_id = request.tab_id
        while True:
            if abort_registry.is_aborted(tab_id) or not abort_registry.is_streaming(
                tab_id
            ):
                if not future.done():
                    future.cancel()
                raise asyncio.CancelledError
            done, _pending = await asyncio.wait({future}, timeout=0.25)
            if future in done:
                return future.result()


# ---------------------------------------------------------------------------
# implementation_plan (multi-Agent discussion coordinator only)
# ---------------------------------------------------------------------------

#: Default workspace path for the coordinator's plan document (see spec
#: ``local://coordinator-plan-spec.md``).  Kept as a module constant so the
#: handler's confirmation string and any future callers agree on ONE name.
_DEFAULT_PLAN_DOC_PATH = "实施计划.md"


class ImplementationPlanToolHandler:
    """One-shot handler for the ``implementation_plan`` tool (coordinator only).

    Registers a LIGHTWEIGHT structured checklist of :class:`FeatureItem`
    entries against the conversation's ``meta["discussion"]["implementation"]``
    blob so the existing pill / drawer / per-item execution UI can drive the
    subsequent implementation runs.  The coordinator (主持人) writes the FULL
    plan document to the workspace (``实施计划.md`` by default) with the
    ``write`` tool; this handler receives only the lightweight items — a
    short ``title``, an ``assigned_role`` the coordinator already picked, and
    optional ``depends_on`` / ``verify_command`` / ``doc_ref`` /
    ``acceptance_criteria`` pointers.  **No text parsing** — every value is
    taken structurally from the tool arguments (contrast with the removed
    :class:`LlmFeatureItemExtractor`, which parsed a JSON blob out of an 8-s
    POST reply).

    Persistence mirrors :meth:`OrchestrateDiscussionUseCase._persist_plan`:
    load the aggregate, ``write_implementation_plan`` onto its live
    ``meta["discussion"]`` blob (§3.1 additive), then travel the ``save``
    (meta) path.  A missing / non-discussion conversation is a no-op error
    string — never raises so the adapter contract (``never raise from a
    handler``) is preserved and the model gets a deterministic result.

    Advertised ONLY in discussion mode: the tool name is added to
    :data:`qai.chat.application.use_cases.tool_advertise.CONDITIONAL_TOOL_NAMES`
    so the main-agent / sub-agent ``compose_advertised_tools`` path drops it
    from their base schema list; the discussion path (``_speaker_tool_schemas``)
    intersects the full registry ``schemas()`` with ``role ∩ mode ∩ global``,
    so a coordinator whose ``allowed_tools`` names ``implementation_plan``
    still sees it.
    """

    __slots__ = ("_conversations", "_clock", "_ids")

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._conversations = conversations
        self._clock = clock
        self._ids = ids

    async def execute(self, request: ToolInvocationRequest) -> str:
        args = request.arguments or {}
        items_raw = args.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            return (
                "[implementation_plan error] 'items' must be a non-empty "
                "array of {title, assigned_role?, depends_on?, "
                "verify_command?, doc_ref?, acceptance_criteria?} objects."
            )
        # Structural validation up-front so the model gets a deterministic
        # error before we touch the aggregate.
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(items_raw):
            if not isinstance(raw, dict):
                return (
                    f"[implementation_plan error] items[{index}] must be an "
                    "object."
                )
            title = raw.get("title")
            if not isinstance(title, str) or not title.strip():
                return (
                    f"[implementation_plan error] items[{index}].title must "
                    "be a non-empty string."
                )
            entry: dict[str, Any] = {"title": title.strip()}
            for opt_str in ("assigned_role", "verify_command", "doc_ref"):
                value = raw.get(opt_str)
                if value is not None:
                    if not isinstance(value, str):
                        return (
                            f"[implementation_plan error] items[{index}]."
                            f"{opt_str} must be a string when present."
                        )
                    stripped = value.strip()
                    if stripped:
                        entry[opt_str] = stripped
            for opt_list in ("depends_on", "acceptance_criteria"):
                value = raw.get(opt_list)
                if value is None:
                    continue
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    return (
                        f"[implementation_plan error] items[{index}]."
                        f"{opt_list} must be a list of strings when present."
                    )
                cleaned = [v.strip() for v in value if v and v.strip()]
                if cleaned:
                    entry[opt_list] = cleaned
            normalized.append(entry)

        # Mint one ULID per item and build a title→id map so ``depends_on``
        # entries the coordinator wrote as titles (natural language, since it
        # does not know the minted ids) resolve to real ids.  An unresolved
        # entry is preserved verbatim (may already BE an id the coordinator
        # obtained from a prior call) — never dropped silently, never raises.
        minted_ids: list[str] = [self._ids.new_id() for _ in normalized]
        title_to_id = {
            entry["title"]: minted_ids[i]
            for i, entry in enumerate(normalized)
        }

        # Load the conversation via the repository so the handler stays
        # self-contained (the orchestrator's ``_persist_plan`` reuses the same
        # discipline: read live blob → merge → set_discussion → save).
        try:
            conv = await self._conversations.find(request.conversation_id)
        except Exception as exc:  # noqa: BLE001 — adapter contract: never raise.
            _log.warning(
                "chat.implementation_plan.load_failed",
                conversation_id=request.conversation_id.value,
                exc_info=True,
            )
            return f"[implementation_plan error] conversation load failed: {exc}"
        if conv is None:
            return (
                "[implementation_plan error] conversation "
                f"{request.conversation_id.value!r} not found."
            )
        discussion = conv.discussion
        if discussion is None:
            return (
                "[implementation_plan error] not a discussion conversation; "
                "no meta['discussion'] blob to write to."
            )

        now_iso = self._clock.now().isoformat()
        existing = read_implementation_plan(discussion)
        created_at = existing.created_at if existing else now_iso
        feature_items: list[FeatureItem] = []
        for entry, item_id in zip(normalized, minted_ids, strict=True):
            deps = entry.get("depends_on") or ()
            resolved_deps = tuple(title_to_id.get(d, d) for d in deps)
            feature_items.append(
                FeatureItem.new(
                    id=item_id,
                    title=entry["title"],
                    assigned_role=entry.get("assigned_role"),
                    depends_on=resolved_deps,
                    verify_command=entry.get("verify_command"),
                    acceptance_criteria=entry.get("acceptance_criteria") or (),
                    # ``doc_ref`` is stored as the description prefix so the
                    # executor's ``_build_implementation_context`` block
                    # surfaces the plan-document pointer without altering the
                    # frozen :class:`FeatureItem` shape (data-model preserved,
                    # per spec § "保留 implementation_plan.py 数据模型").
                    description=(
                        f"详细见 {args.get('plan_doc_path') or _DEFAULT_PLAN_DOC_PATH}"
                        f" § {entry['doc_ref']}"
                        if entry.get("doc_ref")
                        else ""
                    ),
                    status="pending",
                )
            )

        plan = ImplementationPlan(
            version=1,
            phase="planned",
            run_id=None,
            current_item=None,
            items=tuple(feature_items),
            created_at=created_at,
            updated_at=now_iso,
            last_error=None,
            stopped_by_user=False,
            paused_at=None,
        )
        new_discussion = write_implementation_plan(discussion, plan)
        try:
            conv.set_discussion(new_discussion, now=self._clock.now())
            await self._conversations.save(conv)
        except Exception as exc:  # noqa: BLE001 — adapter contract: never raise.
            _log.warning(
                "chat.implementation_plan.persist_failed",
                conversation_id=request.conversation_id.value,
                exc_info=True,
            )
            return f"[implementation_plan error] plan persist failed: {exc}"

        _log.info(
            "chat.implementation_plan.registered",
            conversation_id=request.conversation_id.value,
            item_count=len(feature_items),
        )
        plan_doc = args.get("plan_doc_path") or _DEFAULT_PLAN_DOC_PATH
        return (
            f"Implementation plan registered: {len(feature_items)} item(s), "
            f"phase=planned. Detailed requirements live in {plan_doc}."
        )
