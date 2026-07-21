# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai conv`` subcommands — chat conversation / tab / experience / snapshot CRUD.

Desktop App Plan §2.1.1 group F1. Thin wrappers around the chat bounded
context's **non-streaming** use cases (``qai.chat.application.use_cases.*``):

* conversation CRUD: ``list / show / rename / delete / compact / generate-title``
* tab management:    ``tab list / tab open / tab close``
* experience library: ``experience list / experience delete / experience categories``
* prompt snapshot:   ``snapshot get / snapshot save``

Streaming / server-only use cases (``StreamChatUseCase`` /
``UploadImageUseCase`` / ``EnhancePromptUseCase`` /
``BuildMemoryContextUseCase``) are **deliberately excluded** — they require
a long-lived API server / WebSocket pipe / inbound HTTP request body.

Behavioural notes
-----------------
* All commands print JSON to stdout (``ensure_ascii=False`` for Chinese
  conversation titles / experience content). Errors / progress logs go to
  stderr per :func:`apps.cli._runtime.cli_container`.
* ``conv generate-title`` requires an LLM provider configured under
  ``settings.chat`` — when offline the use case falls back to a heuristic
  title; the CLI surfaces both via the ``used_fallback`` field so an
  operator can see whether the LLM round-trip actually happened.
* ``conv delete`` is double-confirmed via ``--yes`` to avoid accidental
  data loss; without it the command exits 2 with a guidance message.
* All identifiers are passed through the corresponding domain VO
  (``ConversationId.of`` / ``TabId.of`` / ``ExperienceId.of``) so input
  validation (length / control-char) is done at the domain boundary, not
  ad-hoc in the CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.api.di import Container
from apps.cli._runtime import run_use_case

__all__ = ["register"]


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _default_json(obj: Any) -> Any:
    """``json.dumps`` fallback for domain dataclasses + datetime / Enum.

    The chat use cases return ``Conversation`` / ``ConversationTab`` /
    ``Experience`` aggregates — frozen / slotted dataclasses with
    nested VOs (``ConversationId(value=...)``). Walk them via ``asdict``
    when possible, fall through to ``__dict__`` / ``str`` for non-
    dataclass domain objects.
    """

    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    # Enum — dump the value.
    value = getattr(obj, "value", None)
    if value is not None and isinstance(value, (str, int)):
        return value
    # Last resort — str() so jq still renders something useful.
    return str(obj)


def _emit(payload: Any) -> None:
    """Pretty-print ``payload`` to stdout as JSON.

    Reconfigures ``sys.stdout`` to UTF-8 once on first call: chat data
    contains Chinese conversation titles / experience content, and
    Windows attaches a cp1252 / cp936 codec to ``sys.stdout`` by default
    which cannot encode arbitrary CJK / emoji.  ``reconfigure(encoding=
    "utf-8", errors="backslashreplace")`` is idempotent (safe across
    multiple emits in one run) and matches what ``apps.api.main`` does
    on startup for log streams.
    """

    if getattr(sys.stdout, "encoding", "").lower() not in {"utf-8", "utf8"}:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 — fall through to legacy codec
            pass
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_default_json)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Path | None]:
    """Pull the global ``--repo-root`` / ``--config`` flags off args."""

    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


# ---------------------------------------------------------------------------
# conv list / show / rename / delete / compact / generate-title
# ---------------------------------------------------------------------------


def _cmd_conv_list(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.conversation_management import (
        ListConversationsInput,
    )

    async def _go(c: Container) -> Any:
        request = ListConversationsInput(
            query=args.query,
            limit=args.limit,
            offset=args.offset,
        )
        return await c.chat.list_conversations_use_case.execute(request)

    rows = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        [
            {
                "id": row.conversation.id.value,
                "title": row.conversation.title,
                "status": row.conversation.status.value
                if hasattr(row.conversation.status, "value")
                else str(row.conversation.status),
                "created_at": row.conversation.created_at.isoformat(),
                "updated_at": row.conversation.updated_at.isoformat(),
                "message_count": row.message_count,
                "round_count": row.round_count,
                "tool_call_count": row.tool_call_count,
                "snippet": row.snippet,
            }
            for row in rows
        ]
    )
    return 0


def _cmd_conv_show(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.conversation_management import (
        GetConversationMessagesInput,
    )
    from qai.chat.domain.ids import ConversationId

    async def _go(c: Container) -> Any:
        request = GetConversationMessagesInput(
            conversation_id=ConversationId.of(args.conversation_id),
            cursor=args.cursor,
            limit=args.limit,
        )
        return await c.chat.get_conversation_messages_use_case.execute(request)

    page = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "next_cursor": page.next_cursor,
            "items": [
                {
                    "id": msg.id.value,
                    "role": msg.role.value
                    if hasattr(msg.role, "value")
                    else str(msg.role),
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat(),
                    "parent_id": msg.parent_id.value if msg.parent_id else None,
                    "tool_calls": list(msg.tool_calls or ()),
                    "tool_results": list(msg.tool_results or ()),
                    "usage": dict(msg.usage or {}),
                    "model_id": msg.model_id,
                    "model_provider": msg.model_provider,
                    "meta": dict(msg.meta or {}),
                }
                for msg in page.items
            ],
        }
    )
    return 0


def _cmd_conv_rename(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.conversation_management import (
        RenameConversationInput,
    )
    from qai.chat.domain.ids import ConversationId

    async def _go(c: Container) -> Any:
        request = RenameConversationInput(
            conversation_id=ConversationId.of(args.conversation_id),
            new_title=args.title,
        )
        return await c.chat.rename_conversation_use_case.execute(request)

    conv = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "id": conv.id.value,
            "title": conv.title,
            "updated_at": conv.updated_at.isoformat(),
        }
    )
    return 0


def _cmd_conv_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        sys.stderr.write(
            "qai conv delete: refused without --yes "
            "(deleting a conversation is permanent).\n"
        )
        return 2

    from qai.chat.application.use_cases.conversation_management import (
        DeleteConversationInput,
    )
    from qai.chat.domain.ids import ConversationId

    async def _go(c: Container) -> Any:
        request = DeleteConversationInput(
            conversation_id=ConversationId.of(args.conversation_id),
        )
        await c.chat.delete_conversation_use_case.execute(request)
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "deleted": args.conversation_id})
    return 0


def _cmd_conv_compact(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.compact import CompactChatInput
    from qai.chat.domain.ids import ConversationId

    async def _go(c: Container) -> Any:
        request = CompactChatInput(
            conversation_id=ConversationId.of(args.conversation_id),
            budget_tokens=args.budget,
            trigger_threshold=args.threshold,
            model_id=args.model_id,
        )
        return await c.chat.compact_chat_use_case.execute(request)

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "needs_compaction": result.needs_compaction,
            "context_size": {
                "used": result.context_size.used.value,
                "budget": result.context_size.budget.value,
            },
        }
    )
    return 0


def _cmd_conv_generate_title(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.title import GenerateTitleInput
    from qai.chat.domain.ids import ConversationId

    async def _go(c: Container) -> Any:
        request = GenerateTitleInput(
            conversation_id=ConversationId.of(args.conversation_id),
            user_message=args.user_message,
            timeout_seconds=args.timeout,
            persist=not args.no_persist,
        )
        return await c.chat.generate_title_use_case.execute(request)

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "title": result.title,
            "used_fallback": result.used_fallback,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# conv tab list / open / close
# ---------------------------------------------------------------------------


def _serialise_tab(tab: Any) -> dict[str, Any]:
    return {
        "id": tab.id.value,
        "conversation_id": tab.conversation_id.value,
        "status": tab.status.value
        if hasattr(tab.status, "value")
        else str(tab.status),
        "created_at": tab.created_at.isoformat(),
        "last_active_at": tab.last_active_at.isoformat(),
    }


def _cmd_conv_tab_list(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await c.chat.list_active_tabs_use_case.execute()

    tabs = run_use_case(_go, **_runtime_kwargs(args))
    _emit([_serialise_tab(t) for t in tabs])
    return 0


def _cmd_conv_tab_open(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.tab_management import OpenTabInput

    async def _go(c: Container) -> Any:
        request = OpenTabInput(
            conversation_id=args.conversation_id,
            tab_id=args.tab_id,
        )
        return await c.chat.open_tab_use_case.execute(request)

    tab = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_tab(tab))
    return 0


def _cmd_conv_tab_close(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.tab_management import CloseTabInput

    async def _go(c: Container) -> Any:
        request = CloseTabInput(tab_id=args.tab_id)
        return await c.chat.close_tab_use_case.execute(request)

    tab = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_tab(tab))
    return 0


# ---------------------------------------------------------------------------
# conv experience list / delete / categories
# ---------------------------------------------------------------------------


def _cmd_conv_experience_list(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.experience_management import (
        ListExperiencesInput,
    )

    async def _go(c: Container) -> Any:
        request = ListExperiencesInput(
            category=args.category,
            limit=args.limit,
        )
        return await c.chat.list_experiences_use_case.execute(request)

    rows = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        [
            {
                "id": e.id.value,
                "category": e.category,
                "content": e.content,
                "created_at": e.created_at.isoformat(),
                "metadata": dict(e.metadata or {}),
            }
            for e in rows
        ]
    )
    return 0


def _cmd_conv_experience_delete(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.experience_management import (
        DeleteExperienceInput,
    )

    async def _go(c: Container) -> Any:
        request = DeleteExperienceInput(experience_id=args.experience_id)
        await c.chat.delete_experience_use_case.execute(request)
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "deleted": args.experience_id})
    return 0


def _cmd_conv_experience_categories(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await c.chat.list_experience_categories_use_case.execute()

    categories = run_use_case(_go, **_runtime_kwargs(args))
    _emit(list(categories))
    return 0


# ---------------------------------------------------------------------------
# conv snapshot get / save
# ---------------------------------------------------------------------------


def _cmd_conv_snapshot_get(args: argparse.Namespace) -> int:
    from qai.chat.application.use_cases.extras import GetPromptSnapshotInput

    async def _go(c: Container) -> Any:
        request = GetPromptSnapshotInput(request_id=args.request_id)
        return await c.chat.get_prompt_snapshot_use_case.execute(request)

    snap = run_use_case(_go, **_runtime_kwargs(args))
    if snap is None:
        _emit(None)
        return 0
    _emit({"request_id": snap.request_id, "payload": snap.payload})
    return 0


def _cmd_conv_snapshot_save(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON: {exc}\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write(
            "invalid JSON: top-level value must be a JSON object "
            f"(got {type(payload).__name__})\n"
        )
        return 2

    from qai.chat.application.use_cases.extras import SavePromptSnapshotInput

    async def _go(c: Container) -> Any:
        request = SavePromptSnapshotInput(
            request_id=args.request_id, payload=payload
        )
        await c.chat.save_prompt_snapshot_use_case.execute(request)
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "request_id": args.request_id})
    return 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai conv`` to the top-level dispatcher."""

    conv = subparsers.add_parser(
        "conv",
        help="chat conversation / tab / experience CRUD (non-streaming)",
        description=(
            "Operate on chat conversations, tabs, the experience library "
            "and prompt snapshots. Streaming chat is intentionally not "
            "exposed here — run ``qai serve`` and use the WS / SSE API."
        ),
    )
    sub = conv.add_subparsers(dest="conv_command", required=True, metavar="<subcommand>")

    # --- list / show / rename / delete / compact / generate-title ---
    p_list = sub.add_parser("list", help="list (and optionally search) conversations")
    p_list.add_argument("--query", default=None, help="free-text search filter")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.set_defaults(handler=_cmd_conv_list)

    p_show = sub.add_parser(
        "show",
        help="print a page of messages for a conversation",
    )
    p_show.add_argument("conversation_id")
    p_show.add_argument(
        "--cursor",
        default=None,
        help="opaque cursor returned by a previous show call",
    )
    p_show.add_argument(
        "--page",
        dest="cursor",
        default=argparse.SUPPRESS,
        help="alias for --cursor (legacy front-end uses 'page')",
    )
    p_show.add_argument("--limit", type=int, default=50)
    p_show.set_defaults(handler=_cmd_conv_show)

    p_rename = sub.add_parser("rename", help="set a new title")
    p_rename.add_argument("conversation_id")
    p_rename.add_argument("title")
    p_rename.set_defaults(handler=_cmd_conv_rename)

    p_delete = sub.add_parser("delete", help="permanently delete a conversation")
    p_delete.add_argument("conversation_id")
    p_delete.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion (required; conversation rows do NOT go to a trash bin)",
    )
    p_delete.set_defaults(handler=_cmd_conv_delete)

    p_compact = sub.add_parser(
        "compact",
        help="evaluate whether a conversation needs compaction (no mutation)",
    )
    p_compact.add_argument("conversation_id")
    p_compact.add_argument(
        "--budget",
        type=int,
        default=128_000,
        help="token budget (default 128000); overridden by --model-id when set",
    )
    p_compact.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="trigger threshold ratio in [0, 1] (default 0.8)",
    )
    p_compact.add_argument("--model-id", default=None)
    p_compact.set_defaults(handler=_cmd_conv_compact)

    p_title = sub.add_parser(
        "generate-title",
        help="generate (and persist) a conversation title via the LLM",
    )
    p_title.add_argument("conversation_id")
    p_title.add_argument(
        "user_message",
        help="the first user message to derive the title from",
    )
    p_title.add_argument("--timeout", type=float, default=10.0)
    p_title.add_argument(
        "--no-persist",
        action="store_true",
        help="compute the title but do not write it back to the conversation",
    )
    p_title.set_defaults(handler=_cmd_conv_generate_title)

    # --- tab ---
    p_tab = sub.add_parser("tab", help="multi-tab session management")
    tab_sub = p_tab.add_subparsers(
        dest="tab_command", required=True, metavar="<subcommand>"
    )

    p_tab_list = tab_sub.add_parser("list", help="list every non-closed tab")
    p_tab_list.set_defaults(handler=_cmd_conv_tab_list)

    p_tab_open = tab_sub.add_parser(
        "open", help="open (or idempotently re-open) a tab on a conversation"
    )
    p_tab_open.add_argument("conversation_id")
    p_tab_open.add_argument(
        "--tab-id",
        default=None,
        help="reuse an existing TabId (page-reload case); omit to mint fresh",
    )
    p_tab_open.set_defaults(handler=_cmd_conv_tab_open)

    p_tab_close = tab_sub.add_parser("close", help="mark a tab as closed")
    p_tab_close.add_argument("tab_id")
    p_tab_close.set_defaults(handler=_cmd_conv_tab_close)

    # --- experience ---
    p_exp = sub.add_parser("experience", help="chat experience library")
    exp_sub = p_exp.add_subparsers(
        dest="experience_command", required=True, metavar="<subcommand>"
    )

    p_exp_list = exp_sub.add_parser("list", help="list experiences (newest first)")
    p_exp_list.add_argument(
        "--category",
        default=None,
        help="restrict to one category (use 'experience categories' to list)",
    )
    p_exp_list.add_argument("--limit", type=int, default=50)
    p_exp_list.set_defaults(handler=_cmd_conv_experience_list)

    p_exp_del = exp_sub.add_parser("delete", help="remove a single experience")
    p_exp_del.add_argument("experience_id")
    p_exp_del.set_defaults(handler=_cmd_conv_experience_delete)

    p_exp_cat = exp_sub.add_parser(
        "categories", help="enumerate every category currently in use"
    )
    p_exp_cat.set_defaults(handler=_cmd_conv_experience_categories)

    # --- snapshot ---
    p_snap = sub.add_parser("snapshot", help="prompt-debug snapshot store")
    snap_sub = p_snap.add_subparsers(
        dest="snapshot_command", required=True, metavar="<subcommand>"
    )

    p_snap_get = snap_sub.add_parser(
        "get", help="look up a captured snapshot by request id"
    )
    p_snap_get.add_argument("request_id")
    p_snap_get.set_defaults(handler=_cmd_conv_snapshot_get)

    p_snap_save = snap_sub.add_parser(
        "save",
        help="persist a snapshot payload (stdin-style JSON object as positional arg)",
    )
    p_snap_save.add_argument("request_id")
    p_snap_save.add_argument(
        "payload",
        help="JSON object literal — quote with single quotes on PowerShell",
    )
    p_snap_save.set_defaults(handler=_cmd_conv_snapshot_save)
