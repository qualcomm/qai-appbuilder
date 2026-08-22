# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Handlers + schemas for the ``remember_experience`` / ``recall_experience``
tools — the agent-editable long-term "lessons" layer.

Design (mirrors ``search_conversations``): the experience store is NOT
auto-injected into the system prompt. Instead the model decides WHEN to
persist a reusable lesson (``remember_experience``) and WHEN to pull the
relevant handful back (``recall_experience``), so a large corpus stays useful
(recall is intent-driven, not a blind always-injected top-N block that misses
the current need and burns context / cache).

* ``remember_experience`` — save / update / delete a distilled lesson
  (decisions, gotchas, user preferences). Backed by
  :class:`SqliteExperienceRepository`.
* ``recall_experience`` — FTS5 keyword recall over past lessons. Backed by
  :class:`SqliteExperienceRecall`.

Both handlers are best-effort: when no repository is wired (legacy / test
stubs) they report gracefully; any failure is caught and turned into a short
tool-result string rather than breaking the turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.adapters.experience_repository import (
        SqliteExperienceRecall,
        SqliteExperienceRepository,
    )
    from qai.chat.application.ports import ToolInvocationRequest


__all__ = [
    "RECALL_EXPERIENCE_TOOL_SCHEMA",
    "REMEMBER_EXPERIENCE_TOOL_SCHEMA",
    "RecallExperienceToolHandler",
    "RememberExperienceToolHandler",
]

_log = get_logger(__name__)

_VALID_ACTIONS = ("save", "update", "delete")
_DEFAULT_RECALL_LIMIT = 5
_MAX_RECALL_LIMIT = 20


REMEMBER_EXPERIENCE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "remember_experience",
        "description": (
            "Persist a reusable LESSON to long-term memory so a FUTURE "
            "conversation can recall it. Use for durable, distilled "
            "knowledge: a non-obvious fix, a project convention you had to "
            "discover, a user preference, a decision and its rationale. Do "
            "NOT use for ephemeral task state or a summary of the current "
            "chat. Capture sparingly and specifically — one strong, "
            "self-contained lesson beats several vague ones. Three actions: "
            "'save' (new lesson), 'update' (revise an existing lesson by id), "
            "'delete' (remove a stale lesson by id)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "update", "delete"],
                    "description": (
                        "'save' a new lesson (default), 'update' an existing "
                        "one by id, or 'delete' one by id."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "save/update: the lesson text — specific and "
                        "self-contained (what, when, why)."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "save: a short tag grouping the lesson (e.g. "
                        "'workflow', 'user_pref', 'gotcha'). Defaults to "
                        "'general'."
                    ),
                },
                "importance": {
                    "type": "number",
                    "description": (
                        "save/update: salience 0.0..1.0 biasing recall "
                        "ranking (default 0.5). Reserve high values for "
                        "lessons that should surface often."
                    ),
                },
                "experience_id": {
                    "type": "string",
                    "description": (
                        "update/delete: the id of the lesson to modify (from "
                        "a prior recall_experience result)."
                    ),
                },
            },
            "required": [],
        },
    },
}


RECALL_EXPERIENCE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recall_experience",
        "description": (
            "Search your long-term LESSONS to recall a distilled insight you "
            "saved before (a fix, convention, user preference, project fact, "
            "or decision). \n"
            "PREFER THIS FIRST — before a code search (grep/glob) or asking "
            "the user — whenever the user refers to something previously told "
            "or agreed: phrasings like \"我之前/我们(的)/上次/还记得…\", "
            "\"our project's …\", \"what did I say about …\", or a project fact "
            "you were told (e.g. a codename, target device, chosen setting, a "
            "preference). Such facts usually live in MEMORY rather than the "
            "code, so check here first; if recall comes back empty, then reach "
            "for code search or other tools as normal. \n"
            "Returns the most relevant lessons with their ids (pass an id to "
            "remember_experience to update/delete a stale one). This searches "
            "distilled lessons, NOT raw past conversations — use "
            "search_conversations for the latter. \n"
            "Matching is KEYWORD-based (substring), not semantic: it will miss "
            "when your query words differ from how the lesson was worded. If a "
            "recall returns NOTHING, try varied keywords FIRST before turning "
            "elsewhere — retry once or twice with different/broader/fewer "
            "keywords and "
            "likely synonyms (e.g. \"发布流程\" → also try \"分支/branch/main/"
            "release\"; a codename → the bare name). Only conclude there is no "
            "saved lesson after a couple of varied attempts come back empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "keywords describing what you want to recall."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "max lessons to return (default 5, max 20)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


def _coerce_limit(value: Any, default: int, maximum: int) -> int:
    """Best-effort positive-int coercion, clamped to ``maximum``."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if out <= 0:
        return default
    return min(out, maximum)


class RememberExperienceToolHandler:
    """Handler for ``remember_experience`` (save / update / delete).

    Best-effort: reports gracefully when no repository is wired, and never
    raises out of :meth:`execute` — a persistence failure becomes a short
    tool-result string so the turn continues.
    """

    __slots__ = ("_repository",)

    def __init__(self, *, repository: SqliteExperienceRepository | None = None) -> None:
        self._repository = repository

    async def execute(self, request: ToolInvocationRequest) -> str:
        repo = self._repository
        if repo is None:
            return (
                "[remember_experience] experience memory is not available in "
                "this environment."
            )
        args = request.arguments if isinstance(request.arguments, dict) else {}
        action = args.get("action") or "save"
        if action not in _VALID_ACTIONS:
            return (
                f"[remember_experience] unknown action {action!r}. Valid "
                "actions: save, update, delete."
            )
        try:
            if action == "save":
                result = await self._save(repo, args)
            elif action == "update":
                result = await self._update(repo, args)
            else:
                result = await self._delete(repo, args)
            _log.info("chat.remember_experience.ok", action=str(action), result=result)
            return result
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.remember_experience.failed",
                action=str(action),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                f"[remember_experience error] the {action} operation failed; "
                "the lesson was not persisted."
            )

    async def _save(
        self, repo: SqliteExperienceRepository, args: dict[str, Any]
    ) -> str:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return "[remember_experience] save needs a non-empty 'content'."
        category = args.get("category")
        category = category if isinstance(category, str) and category.strip() else "general"
        importance = args.get("importance", 0.5)
        exp_id = await repo.save(
            category=category, content=content, importance=importance
        )
        return f"Saved experience {exp_id} (category={category})."

    async def _update(
        self, repo: SqliteExperienceRepository, args: dict[str, Any]
    ) -> str:
        exp_id = args.get("experience_id")
        if not isinstance(exp_id, str) or not exp_id.strip():
            return "[remember_experience] update needs an 'experience_id'."
        content = args.get("content")
        content = content if isinstance(content, str) and content.strip() else None
        importance = args.get("importance")
        if content is None and importance is None:
            return (
                "[remember_experience] update needs 'content' and/or "
                "'importance' to change."
            )
        changed = await repo.update(
            experience_id=exp_id, content=content, importance=importance
        )
        if not changed:
            return f"[remember_experience] no experience with id {exp_id!r}."
        return f"Updated experience {exp_id}."

    async def _delete(
        self, repo: SqliteExperienceRepository, args: dict[str, Any]
    ) -> str:
        exp_id = args.get("experience_id")
        if not isinstance(exp_id, str) or not exp_id.strip():
            return "[remember_experience] delete needs an 'experience_id'."
        changed = await repo.delete(experience_id=exp_id)
        if not changed:
            return f"[remember_experience] no experience with id {exp_id!r}."
        return f"Deleted experience {exp_id}."


class RecallExperienceToolHandler:
    """Handler for ``recall_experience`` (FTS5 recall over saved lessons).

    Best-effort: reports gracefully when no recall backend is wired, and
    never raises out of :meth:`execute`.
    """

    __slots__ = ("_recall",)

    def __init__(self, *, recall: SqliteExperienceRecall | None = None) -> None:
        self._recall = recall

    async def execute(self, request: ToolInvocationRequest) -> str:
        recall = self._recall
        if recall is None:
            return (
                "[recall_experience] experience memory is not available in "
                "this environment."
            )
        args = request.arguments if isinstance(request.arguments, dict) else {}
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "[recall_experience] needs a 'query' string."
        limit = _coerce_limit(args.get("limit"), _DEFAULT_RECALL_LIMIT, _MAX_RECALL_LIMIT)
        try:
            hits = await recall.recall(query=query, limit=limit)
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.recall_experience.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                "[recall_experience error] the query failed; try a different "
                "query."
            )
        _log.info(
            "chat.recall_experience.ok",
            query=query,
            hit_count=len(hits),
        )
        if not hits:
            return (
                f"No saved lessons matched {query!r}. Nothing to recall for "
                "this query."
            )
        lines = [
            f"{len(hits)} lesson(s) for {query!r} (most relevant first). Pass "
            "an experience_id to remember_experience to update/delete one:",
        ]
        for ordinal, hit in enumerate(hits, start=1):
            category = hit.category or "general"
            lines.append(
                f"{ordinal}. [{category}] {hit.content or '(empty)'}\n"
                f"   experience_id: {hit.experience_id} "
                f"| importance: {hit.importance:.2f} | when: {hit.created_at}"
            )
        return "\n".join(lines)
