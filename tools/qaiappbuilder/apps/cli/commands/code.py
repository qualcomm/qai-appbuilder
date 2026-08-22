# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai code`` subcommands - ai_coding session / config / oc / skill / checkpoint CRUD.

Desktop App Plan §2.1.1 group F2. Thin wrappers around the ai_coding bounded
context's **non-streaming** use cases (``qai.ai_coding.application.use_cases.*``).

Streaming / interactive use cases (``StreamCodingSessionUseCase`` /
``SendUserMessageUseCase``) are intentionally excluded - they require a
long-lived API server pipe.

Layout
------
The command tree is wide enough that flat registration would be unreadable;
instead each sub-group has its own ``_register_<group>(parent)`` helper and
``register()`` calls them in sequence:

* ``code session ...``     - 14 use cases (rename / effort / notify / ...)
* ``code config ...``      - get / set the UI config (CC + OC, ``--provider`` switches)
* ``code creds ...``       - SecretStore-backed credentials (CC + OC)
* ``code oc ...``          - 4 OC subprocess control verbs
* ``code skill ...``       - register / list
* ``code checkpoint ...``  - create / list / rewind
* ``code context ...``     - usage / size
* ``code health``          - folded providers + models + auth
* ``code perm expire``     - sweep stale pending permission requests

Design notes
------------
* All identifiers go through their domain VO (``CodingSessionId(value=...)``)
  so input validation lives at the domain boundary.
* Credentials use ``--value-stdin`` to avoid leaking secrets into shell
  history; the operator pipes the value via stdin.
* JSON-shaped use case inputs (skill spec, config doc) are accepted as
  positional JSON literals and parsed via ``json.loads``; an invalid
  literal exits 2 with a clear stderr message.
* Some use cases take richly-typed dataclass inputs (e.g.
  ``RegisterSkillCommand`` wraps a :class:`Skill` with ``spec: dict``);
  the CLI accepts the JSON form rather than synthesising flag-by-flag,
  matching the use-case contract verbatim.
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
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    value = getattr(obj, "value", None)
    if value is not None and isinstance(value, (str, int)):
        return value
    return str(obj)


def _emit(payload: Any) -> None:
    """Pretty-print ``payload`` to stdout as JSON.

    Reconfigures ``sys.stdout`` to UTF-8 once on first call: ai_coding
    data may carry session titles / workspace paths with non-ASCII
    characters, and Windows attaches a cp1252 / cp936 codec to
    ``sys.stdout`` by default which cannot encode arbitrary CJK / emoji.
    ``reconfigure(encoding="utf-8", errors="backslashreplace")`` is
    idempotent (safe across multiple emits in one run).
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
    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


def _parse_json_object(raw: str, *, what: str) -> dict[str, Any] | None:
    """Parse ``raw`` as a JSON object; emit error + return None on failure."""

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON ({what}): {exc}\n")
        return None
    if not isinstance(parsed, dict):
        sys.stderr.write(
            f"invalid JSON ({what}): top-level value must be an object, "
            f"got {type(parsed).__name__}\n"
        )
        return None
    return parsed


def _serialise_session(session: Any) -> dict[str, Any]:
    """Compact dict-of-strings projection of a ``CodingSession`` aggregate."""

    return {
        "session_id": session.session_id.value,
        "provider": session.provider.value,
        "workspace": session.workspace.path,
        "title": session.title,
        "status": session.status.value,
        "created_at": session.created_at.isoformat(),
        "terminated_at": session.terminated_at.isoformat()
        if session.terminated_at
        else None,
        "termination_reason": session.termination_reason,
        "message_count": len(session.messages),
        "claude_session_id": session.claude_session_id,
        "wechat_notify_user_id": session.wechat_notify_user_id,
        "feishu_notify_user_id": session.feishu_notify_user_id,
        "context_window": session.context_window,
        "total_input_tokens": session.total_input_tokens,
        "total_output_tokens": session.total_output_tokens,
        "total_tool_calls": session.total_tool_calls,
        "last_input_tokens": session.last_input_tokens,
    }


# ===========================================================================
# Session subgroup (14 use cases)
# ===========================================================================


def _cmd_session_list(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.list_coding_sessions import (
        ListCodingSessionsQuery,
    )

    async def _go(c: Container) -> Any:
        return await c.ai_coding.list_coding_sessions_use_case.execute(
            ListCodingSessionsQuery(scope=args.scope)
        )

    rows = run_use_case(_go, **_runtime_kwargs(args))
    _emit([_serialise_session(s) for s in rows])
    return 0


def _cmd_session_show(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.get_coding_session import (
        GetCodingSessionQuery,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_coding_session_use_case.execute(
            GetCodingSessionQuery(session_id=CodingSessionId(value=args.session_id))
        )

    session = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_session(session))
    return 0


def _cmd_session_history(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.get_session_history import (
        GetSessionHistoryQuery,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_session_history_use_case.execute(
            GetSessionHistoryQuery(session_id=CodingSessionId(value=args.session_id))
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "session_id": result.session_id.value,
            "messages": [{"text": m.text} for m in result.messages],
        }
    )
    return 0


def _cmd_session_rename(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.rename_session import (
        RenameSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.rename_session_use_case.execute(
            RenameSessionCommand(
                session_id=CodingSessionId(value=args.session_id),
                new_title=args.title,
            )
        )

    session = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_session(session))
    return 0


def _cmd_session_activate(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.set_active_session import (
        SetActiveSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.set_active_session_use_case.execute(
            SetActiveSessionCommand(
                session_id=CodingSessionId(value=args.session_id)
            )
        )

    session = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "session_id": session.session_id.value, "active": True})
    return 0


def _cmd_session_effort(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.set_session_effort import (
        SetSessionEffortCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    effort: str | None = args.level
    if effort == "none":
        effort = None

    async def _go(c: Container) -> Any:
        return await c.ai_coding.set_session_effort_use_case.execute(
            SetSessionEffortCommand(
                session_id=CodingSessionId(value=args.session_id),
                effort=effort,
            )
        )

    session = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_session(session))
    return 0


def _cmd_session_notify(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.set_session_notify import (
        SetSessionNotifyCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    if args.toggle == "off":
        user_id: str | None = None
    else:
        user_id = args.toggle  # treat the literal as the user id

    async def _go(c: Container) -> Any:
        return await c.ai_coding.set_session_notify_use_case.execute(
            SetSessionNotifyCommand(
                session_id=CodingSessionId(value=args.session_id),
                channel=args.channel,
                user_id=user_id,
            )
        )

    session = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_session(session))
    return 0


def _cmd_session_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        sys.stderr.write(
            "qai code session delete: refused without --yes "
            "(hard-deletes the row + releases the workspace lock).\n"
        )
        return 2
    from qai.ai_coding.application.use_cases.hard_delete_session import (
        HardDeleteSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        await c.ai_coding.hard_delete_session_use_case.execute(
            HardDeleteSessionCommand(
                session_id=CodingSessionId(value=args.session_id)
            )
        )
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "deleted": args.session_id})
    return 0


def _cmd_session_truncate(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.truncate_history import (
        TruncateHistoryCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.truncate_history_use_case.execute(
            TruncateHistoryCommand(
                session_id=CodingSessionId(value=args.session_id),
                marker_index=args.marker_index,
                include_self=args.include_self,
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"removed": result.removed, "remaining": result.remaining})
    return 0


def _cmd_session_workspace(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.change_workspace import (
        ChangeWorkspaceCommand,
    )
    from qai.ai_coding.domain import CodingSessionId, Workspace

    async def _go(c: Container) -> Any:
        await c.ai_coding.change_workspace_use_case.execute(
            ChangeWorkspaceCommand(
                session_id=CodingSessionId(value=args.session_id),
                new_workspace=Workspace(path=args.path),
            )
        )
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "session_id": args.session_id, "workspace": args.path})
    return 0


def _cmd_session_terminate(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.terminate_coding_session import (
        TerminateCodingSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        await c.ai_coding.terminate_coding_session_use_case.execute(
            TerminateCodingSessionCommand(
                session_id=CodingSessionId(value=args.session_id),
                reason=args.reason,
            )
        )
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "session_id": args.session_id, "terminated": True})
    return 0


def _cmd_session_interrupt(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.interrupt_session import (
        InterruptSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.interrupt_session_use_case.execute(
            InterruptSessionCommand(
                session_id=CodingSessionId(value=args.session_id)
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"interrupted": result.interrupted, "reason": result.reason})
    return 0


def _cmd_session_abort(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.abort_revert import (
        AbortSessionCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.abort_session_use_case.execute(
            AbortSessionCommand(
                session_id=CodingSessionId(value=args.session_id)
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"aborted": result.aborted, "reason": result.reason})
    return 0


def _cmd_session_revert(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.abort_revert import (
        RevertMessageCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.revert_message_use_case.execute(
            RevertMessageCommand(
                session_id=CodingSessionId(value=args.session_id),
                marker_index=args.marker_index,
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"removed": result.removed, "remaining": result.remaining})
    return 0


def _register_session(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("session", help="coding session CRUD + lifecycle")
    sub = p.add_subparsers(dest="session_command", required=True, metavar="<subcommand>")

    p_list = sub.add_parser("list", help="list coding sessions")
    p_list.add_argument(
        "--scope",
        choices=("active", "all"),
        default="active",
        help="'active' = non-terminated only (default); 'all' = full history",
    )
    p_list.set_defaults(handler=_cmd_session_list)

    p_show = sub.add_parser("show", help="print one coding session")
    p_show.add_argument("session_id")
    p_show.set_defaults(handler=_cmd_session_show)

    p_history = sub.add_parser("history", help="print the message history of a session")
    p_history.add_argument("session_id")
    p_history.set_defaults(handler=_cmd_session_history)

    p_rename = sub.add_parser("rename", help="set a session's display title")
    p_rename.add_argument("session_id")
    p_rename.add_argument("title")
    p_rename.set_defaults(handler=_cmd_session_rename)

    p_activate = sub.add_parser(
        "activate",
        help="mark a session as the focused one (notify routing)",
    )
    p_activate.add_argument("session_id")
    p_activate.set_defaults(handler=_cmd_session_activate)

    p_effort = sub.add_parser(
        "effort",
        help="adjust per-session thinking depth (low/medium/high/max/none)",
    )
    p_effort.add_argument("session_id")
    p_effort.add_argument(
        "level",
        choices=("low", "medium", "high", "max", "none"),
        help="'none' clears the override and the global default applies",
    )
    p_effort.set_defaults(handler=_cmd_session_effort)

    p_notify = sub.add_parser(
        "notify",
        help="bind / clear a wechat or feishu notify target",
    )
    p_notify.add_argument("session_id")
    p_notify.add_argument(
        "channel",
        choices=("wechat", "feishu"),
    )
    p_notify.add_argument(
        "toggle",
        help="'off' to clear the binding, otherwise the user-id / open-id to bind",
    )
    p_notify.set_defaults(handler=_cmd_session_notify)

    p_delete = sub.add_parser(
        "delete",
        help="permanently delete a session (terminates first if live)",
    )
    p_delete.add_argument("session_id")
    p_delete.add_argument("--yes", action="store_true")
    p_delete.set_defaults(handler=_cmd_session_delete)

    p_trunc = sub.add_parser(
        "truncate",
        help="drop messages after a given history index (Edit-and-Resend mode)",
    )
    p_trunc.add_argument("session_id")
    p_trunc.add_argument(
        "marker_index",
        type=int,
        help="0-based index of the anchor message",
    )
    p_trunc.add_argument(
        "--include-self",
        action="store_true",
        help="also drop the anchor message itself (default: keep it)",
    )
    p_trunc.set_defaults(handler=_cmd_session_truncate)

    p_ws = sub.add_parser("workspace", help="change a session's working directory")
    p_ws.add_argument("session_id")
    p_ws.add_argument("path")
    p_ws.set_defaults(handler=_cmd_session_workspace)

    p_term = sub.add_parser("terminate", help="end a session (status -> TERMINATED)")
    p_term.add_argument("session_id")
    p_term.add_argument("--reason", default="user_request")
    p_term.set_defaults(handler=_cmd_session_terminate)

    p_int = sub.add_parser(
        "interrupt",
        help="soft-interrupt the in-flight turn (no hard abort)",
    )
    p_int.add_argument("session_id")
    p_int.set_defaults(handler=_cmd_session_interrupt)

    p_abort = sub.add_parser(
        "abort",
        help="hard-abort the in-flight turn (force terminate provider)",
    )
    p_abort.add_argument("session_id")
    p_abort.set_defaults(handler=_cmd_session_abort)

    p_revert = sub.add_parser(
        "revert",
        help="revert a session's history past a given index (drops the marker too)",
    )
    p_revert.add_argument("session_id")
    p_revert.add_argument(
        "marker_index",
        type=int,
        help="0-based index of the message to revert past",
    )
    p_revert.set_defaults(handler=_cmd_session_revert)


# ===========================================================================
# Config subgroup (CC + OC, dispatched by --provider)
# ===========================================================================


def _config_uc(c: Container, provider: str, *, save: bool) -> Any:
    """Pick the right CC / OC config use case off the namespace."""

    if provider == "cc":
        return (
            c.ai_coding.save_coding_config_use_case
            if save
            else c.ai_coding.get_coding_config_use_case
        )
    return (
        c.ai_coding.save_oc_coding_config_use_case
        if save
        else c.ai_coding.get_oc_coding_config_use_case
    )


def _cmd_config_get(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_coding_config import (
        GetCodingConfigQuery,
    )

    async def _go(c: Container) -> Any:
        return await _config_uc(c, args.provider, save=False).execute(
            GetCodingConfigQuery()
        )

    doc = run_use_case(_go, **_runtime_kwargs(args))
    _emit(doc)
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_coding_config import (
        SaveCodingConfigCommand,
    )

    parsed = _parse_json_object(args.value, what="config doc")
    if parsed is None:
        return 2

    async def _go(c: Container) -> Any:
        return await _config_uc(c, args.provider, save=True).execute(
            SaveCodingConfigCommand(updates=parsed)
        )

    doc = run_use_case(_go, **_runtime_kwargs(args))
    _emit(doc)
    return 0


def _register_config(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser(
        "config",
        help="ai_coding UI config doc (CC or OC, --provider switches)",
    )
    sub = p.add_subparsers(dest="config_command", required=True, metavar="<subcommand>")

    p_get = sub.add_parser("get", help="print the config document")
    p_get.add_argument("--provider", choices=("cc", "oc"), required=True)
    p_get.set_defaults(handler=_cmd_config_get)

    p_set = sub.add_parser(
        "set",
        help="upsert the config document (shallow merge per use-case contract)",
    )
    p_set.add_argument("--provider", choices=("cc", "oc"), required=True)
    p_set.add_argument(
        "value",
        help="JSON object literal to merge into the document",
    )
    p_set.set_defaults(handler=_cmd_config_set)


# ===========================================================================
# Creds subgroup (CC + OC; SecretStore-backed)
# ===========================================================================


def _creds_get_uc(c: Container, provider: str) -> Any:
    return (
        c.ai_coding.get_coding_credentials_use_case
        if provider == "cc"
        else c.ai_coding.get_oc_coding_credentials_use_case
    )


def _creds_save_uc(c: Container, provider: str) -> Any:
    return (
        c.ai_coding.save_coding_credentials_use_case
        if provider == "cc"
        else c.ai_coding.save_oc_coding_credentials_use_case
    )


def _creds_delete_uc(c: Container, provider: str) -> Any:
    return (
        c.ai_coding.delete_credential_use_case
        if provider == "cc"
        else c.ai_coding.delete_oc_credential_use_case
    )


def _cmd_creds_list(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await _creds_get_uc(c, args.provider).execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        [
            {
                "var_name": s.var_name,
                "in_store": s.in_store,
                "in_env": s.in_env,
                "configured": s.configured,
            }
            for s in result.statuses
        ]
    )
    return 0


def _cmd_creds_set(args: argparse.Namespace) -> int:
    # Read secret from stdin so it never appears in shell history / argv.
    raw_value = sys.stdin.read()
    # Trim trailing newline produced by shell heredocs / `echo`. Embedded
    # newlines inside the secret survive (rare but legal: e.g. JSON
    # service-account blobs for Vertex AI).
    if raw_value.endswith("\n"):
        raw_value = raw_value[:-1]
    if raw_value.endswith("\r"):
        raw_value = raw_value[:-1]

    from qai.ai_coding.application.use_cases.manage_coding_credentials import (
        SaveCodingCredentialsCommand,
    )

    async def _go(c: Container) -> Any:
        return await _creds_save_uc(c, args.provider).execute(
            SaveCodingCredentialsCommand(credentials={args.key: raw_value})
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "saved": list(result.saved),
            "deleted": list(result.deleted),
            "skipped": list(result.skipped),
        }
    )
    return 0


def _cmd_creds_delete(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_coding_credentials import (
        DeleteCredentialCommand,
    )

    async def _go(c: Container) -> Any:
        await _creds_delete_uc(c, args.provider).execute(
            DeleteCredentialCommand(var_name=args.key)
        )
        return None

    run_use_case(_go, **_runtime_kwargs(args))
    _emit({"ok": True, "deleted": args.key})
    return 0


def _register_creds(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser(
        "creds",
        help="ai_coding credentials (SecretStore-backed; CC + OC)",
    )
    sub = p.add_subparsers(dest="creds_command", required=True, metavar="<subcommand>")

    p_list = sub.add_parser("list", help="list credential variable status")
    p_list.add_argument("--provider", choices=("cc", "oc"), required=True)
    p_list.set_defaults(handler=_cmd_creds_list)

    p_set = sub.add_parser(
        "set",
        help="save a credential value read from stdin (use --value-stdin)",
    )
    p_set.add_argument("--provider", choices=("cc", "oc"), required=True)
    p_set.add_argument("--key", required=True, help="credential variable name")
    p_set.add_argument(
        "--value-stdin",
        action="store_true",
        required=True,
        help="confirm that the value will be read from stdin (avoids argv leakage)",
    )
    p_set.set_defaults(handler=_cmd_creds_set)

    p_del = sub.add_parser("delete", help="delete a single credential")
    p_del.add_argument("--provider", choices=("cc", "oc"), required=True)
    p_del.add_argument("--key", required=True)
    p_del.set_defaults(handler=_cmd_creds_delete)


# ===========================================================================
# OC service control (4 verbs)
# ===========================================================================


def _serialise_oc_status(status: Any) -> dict[str, Any]:
    return {
        "running": status.running,
        "pid": status.pid,
        "uptime_seconds": getattr(status, "uptime_seconds", None),
        "port": getattr(status, "port", None),
        "cli_path": getattr(status, "cli_path", ""),
        "external": getattr(status, "external", False),
    }


def _cmd_oc_status(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_oc_service_status_use_case.execute()

    status = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_oc_status(status))
    return 0


def _cmd_oc_start(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await c.ai_coding.start_oc_service_use_case.execute()

    status = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_oc_status(status))
    return 0


def _cmd_oc_stop(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_oc_service import (
        StopOcServiceCommand,
    )

    async def _go(c: Container) -> Any:
        return await c.ai_coding.stop_oc_service_use_case.execute(
            StopOcServiceCommand(force=args.force)
        )

    status = run_use_case(_go, **_runtime_kwargs(args))
    _emit(_serialise_oc_status(status))
    return 0


def _cmd_oc_logs(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_oc_service import (
        GetOcServiceLogsQuery,
    )

    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_oc_service_logs_use_case.execute(
            GetOcServiceLogsQuery(last_n=args.lines)
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"lines": list(result.lines)})
    return 0


def _register_oc(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("oc", help="OpenCode local service subprocess control")
    sub = p.add_subparsers(dest="oc_command", required=True, metavar="<subcommand>")

    p_status = sub.add_parser("status", help="report OC service status")
    p_status.set_defaults(handler=_cmd_oc_status)

    p_start = sub.add_parser("start", help="start the OC service subprocess (idempotent)")
    p_start.set_defaults(handler=_cmd_oc_start)

    p_stop = sub.add_parser("stop", help="stop the OC service subprocess")
    p_stop.add_argument(
        "--force",
        action="store_true",
        help="hard-kill (SIGKILL / TerminateProcess); default sends graceful signal",
    )
    p_stop.set_defaults(handler=_cmd_oc_stop)

    p_logs = sub.add_parser("logs", help="print recent OC service log lines")
    p_logs.add_argument("--lines", type=int, default=100)
    p_logs.set_defaults(handler=_cmd_oc_logs)


# ===========================================================================
# Skill subgroup (register / list)
# ===========================================================================


def _cmd_skill_register(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_skills import (
        RegisterSkillCommand,
    )
    from qai.ai_coding.domain import Skill

    parsed = _parse_json_object(args.spec_json, what="skill spec")
    if parsed is None:
        return 2
    name = parsed.get("name")
    description = parsed.get("description")
    if not isinstance(name, str) or not name:
        sys.stderr.write("invalid JSON (skill spec): 'name' must be a non-empty string\n")
        return 2
    if not isinstance(description, str):
        sys.stderr.write("invalid JSON (skill spec): 'description' must be a string\n")
        return 2
    spec = parsed.get("spec", {})
    if not isinstance(spec, dict):
        sys.stderr.write("invalid JSON (skill spec): 'spec' must be an object\n")
        return 2

    async def _go(c: Container) -> Any:
        return await c.ai_coding.register_skill_use_case.execute(
            RegisterSkillCommand(
                skill=Skill(name=name, description=description, spec=spec)
            )
        )

    skill = run_use_case(_go, **_runtime_kwargs(args))
    _emit({"name": skill.name, "description": skill.description, "spec": dict(skill.spec)})
    return 0


def _cmd_skill_list(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> Any:
        return await c.ai_coding.discover_skills_use_case.execute()

    skills = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        [
            {"name": s.name, "description": s.description, "spec": dict(s.spec)}
            for s in skills
        ]
    )
    return 0


def _register_skill(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("skill", help="ai_coding skill registry")
    sub = p.add_subparsers(dest="skill_command", required=True, metavar="<subcommand>")

    p_reg = sub.add_parser(
        "register",
        help='register a skill from a JSON object {"name":..., "description":..., "spec":{...}}',
    )
    p_reg.add_argument("spec_json", help="JSON object literal")
    p_reg.set_defaults(handler=_cmd_skill_register)

    p_list = sub.add_parser("list", help="list discovered skills")
    p_list.set_defaults(handler=_cmd_skill_list)


# ===========================================================================
# Checkpoint subgroup
# ===========================================================================


def _cmd_checkpoint_create(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_checkpoints import (
        CreateCheckpointCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.create_checkpoint_use_case.execute(
            CreateCheckpointCommand(
                session_id=CodingSessionId(value=args.session_id),
                label=args.note,
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    info = result.checkpoint
    _emit(
        {
            "checkpoint_id": info.checkpoint_id,
            "created_at": info.created_at,
            "label": info.label,
            "message_count": info.message_count,
        }
    )
    return 0


def _cmd_checkpoint_list(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_checkpoints import (
        ListCheckpointsQuery,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.list_checkpoints_use_case.execute(
            ListCheckpointsQuery(
                session_id=CodingSessionId(value=args.session_id)
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        [
            {
                "checkpoint_id": cp.checkpoint_id,
                "created_at": cp.created_at,
                "label": cp.label,
                "message_count": cp.message_count,
            }
            for cp in result.checkpoints
        ]
    )
    return 0


def _cmd_checkpoint_rewind(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.manage_checkpoints import (
        RewindCheckpointCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.rewind_checkpoint_use_case.execute(
            RewindCheckpointCommand(
                session_id=CodingSessionId(value=args.session_id),
                checkpoint_id=args.checkpoint_id,
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "checkpoint_id": result.checkpoint_id,
            "removed": result.removed,
            "remaining": result.remaining,
            "files_rewound": result.files_rewound,
        }
    )
    return 0


def _register_checkpoint(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("checkpoint", help="per-session history checkpoints")
    sub = p.add_subparsers(
        dest="checkpoint_command", required=True, metavar="<subcommand>"
    )

    p_create = sub.add_parser("create", help="snapshot the current history")
    p_create.add_argument("session_id")
    p_create.add_argument("--note", default=None, help="optional human-readable label")
    p_create.set_defaults(handler=_cmd_checkpoint_create)

    p_list = sub.add_parser("list", help="enumerate checkpoints for a session")
    p_list.add_argument("session_id")
    p_list.set_defaults(handler=_cmd_checkpoint_list)

    p_rewind = sub.add_parser(
        "rewind",
        help="restore a session to the message count of a checkpoint",
    )
    p_rewind.add_argument("session_id")
    p_rewind.add_argument("checkpoint_id")
    p_rewind.set_defaults(handler=_cmd_checkpoint_rewind)


# ===========================================================================
# Context / health / perm
# ===========================================================================


def _cmd_context_usage(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.query_context_usage import (
        ContextUsageQuery,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_context_usage_use_case.execute(
            ContextUsageQuery(session_id=CodingSessionId(value=args.session_id))
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "ok": result.ok,
            "total_tokens": result.total_tokens,
            "max_tokens": result.max_tokens,
            "percentage": result.percentage,
        }
    )
    return 0


def _cmd_context_size(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.query_context_usage import (
        ContextUsageQuery,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.get_context_size_use_case.execute(
            ContextUsageQuery(session_id=CodingSessionId(value=args.session_id))
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "last_input_tokens": result.last_input_tokens,
            "total_input_tokens": result.total_input_tokens,
            "total_output_tokens": result.total_output_tokens,
            "total_tool_calls": result.total_tool_calls,
            "turn_count": result.turn_count,
            "context_limit": result.context_limit,
            "usage_pct": result.usage_pct,
            "model": result.model,
        }
    )
    return 0


def _register_context(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("context", help="context-window usage queries")
    sub = p.add_subparsers(
        dest="context_command", required=True, metavar="<subcommand>"
    )
    p_usage = sub.add_parser(
        "usage",
        help="CC-flavoured usage report (legacy /api/cc/sessions/{id}/context_usage)",
    )
    p_usage.add_argument("session_id")
    p_usage.set_defaults(handler=_cmd_context_usage)

    p_size = sub.add_parser(
        "size",
        help="OC-flavoured size report (legacy /api/oc/sessions/{id}/context_size)",
    )
    p_size.add_argument("session_id")
    p_size.set_defaults(handler=_cmd_context_size)


def _cmd_health(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.health_status import (
        HealthStatusQuery,
    )
    from qai.ai_coding.domain import Provider

    provider = (
        Provider.CLAUDE_CODE if args.provider == "cc" else Provider.OPEN_CODE
    )

    async def _go(c: Container) -> Any:
        return await c.ai_coding.health_status_use_case.execute(
            HealthStatusQuery(provider=provider, refresh=args.refresh)
        )

    r = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "provider": r.provider,
            "available": r.available,
            "available_providers": list(r.available_providers),
            "providers": [
                {"id": p.id, "name": p.name, "available": p.available}
                for p in r.providers
            ],
            "models": [
                {"id": m.id, "name": m.name, "provider_id": m.provider_id}
                for m in r.models
            ],
            "sdk_available": r.sdk_available,
            "sdk_version": r.sdk_version,
            "auth_configured": r.auth_configured,
            "auth_source": r.auth_source,
            "active_sessions": r.active_sessions,
            "total_sessions": r.total_sessions,
            "models_source": r.models_source,
            "models_base_url": r.models_base_url,
            "models_base_url_source": r.models_base_url_source,
            "models_error": r.models_error,
            "models_cached_age": r.models_cached_age,
        }
    )
    return 0


def _cmd_perm_expire(args: argparse.Namespace) -> int:
    from qai.ai_coding.application.use_cases.expire_stale_permissions import (
        ExpireStalePermissionsCommand,
    )
    from qai.ai_coding.domain import CodingSessionId

    async def _go(c: Container) -> Any:
        return await c.ai_coding.expire_stale_permissions_use_case.execute(
            ExpireStalePermissionsCommand(
                session_id=CodingSessionId(value=args.session_id)
            )
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit(
        {
            "expired_count": result.expired_count,
            "expired_request_ids": list(result.expired_request_ids),
        }
    )
    return 0


# ===========================================================================
# Top-level register
# ===========================================================================


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai code`` to the top-level dispatcher."""

    code = subparsers.add_parser(
        "code",
        help="ai_coding (Claude Code / OpenCode) session + config CRUD",
        description=(
            "Operate on Claude Code (CC) and OpenCode (OC) sessions, config, "
            "credentials, the OC subprocess, skills, checkpoints and context. "
            "Streaming chat / tool exec is intentionally not exposed - run "
            "``qai serve`` for the WS / SSE API."
        ),
    )
    sub = code.add_subparsers(
        dest="code_command", required=True, metavar="<subcommand>"
    )

    _register_session(sub)
    _register_config(sub)
    _register_creds(sub)
    _register_oc(sub)
    _register_skill(sub)
    _register_checkpoint(sub)
    _register_context(sub)

    p_health = sub.add_parser(
        "health",
        help="folded provider + model + auth status (CC or OC)",
    )
    p_health.add_argument(
        "--provider",
        choices=("cc", "oc"),
        default="cc",
        help="primary provider for the response (default cc)",
    )
    p_health.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the model catalog cache and re-enumerate upstream",
    )
    p_health.set_defaults(handler=_cmd_health)

    p_perm = sub.add_parser("perm", help="permission gate utilities")
    perm_sub = p_perm.add_subparsers(
        dest="perm_command", required=True, metavar="<subcommand>"
    )
    p_perm_exp = perm_sub.add_parser(
        "expire",
        help="auto-reject pending permission requests past TTL for a session",
    )
    p_perm_exp.add_argument("session_id")
    p_perm_exp.set_defaults(handler=_cmd_perm_expire)
