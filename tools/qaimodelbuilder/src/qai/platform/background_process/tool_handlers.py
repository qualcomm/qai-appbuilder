# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM tool dispatch handler for the background-process manager.

Implements :func:`handle_background_process`, the async entry point
that the ``apps/api`` tool registry calls when an LLM invokes the
``background_process`` tool.  Translates an LLM-shaped ``params`` dict
into :class:`BackgroundProcessManagerPort` method calls and converts
the typed return values back into a JSON-serializable response dict
following the project's tool-handler convention
(``{"ok": bool, "message": str, ...}`` — see e.g.
``src/qai/ai_coding/infrastructure/tools/handlers/exec.py``).

Error handling
--------------

* Cross-field schema validation (``validate_params``) is run **before**
  any manager call.  Failures raise :class:`ValueError`; callers in the
  tool registry are expected to surface this to the LLM.
* Manager-raised exceptions (:class:`InvalidReadyPattern` /
  :class:`ReadyPortInUse` / :class:`ManagerError`) are caught and
  rendered as ``{"ok": False, "error_code": ..., "message": ...}``
  so the LLM gets a structured failure it can reason about.
* Missing-id lookups (``status`` / ``logs`` / ``stop`` / ``restart``
  on an unknown id) return ``{"ok": False,
  "error_code": "process_not_found", ...}`` — the manager methods
  themselves return ``None`` (a nullable contract),
  so we materialise the error here.

No imports from ``qai.ai_coding.*`` — this module is a pure-platform
artefact.  The ``apps/api`` layer is responsible for wiring this
handler into whatever tool registry it uses.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .ports import (
    BackgroundProcessManagerPort,
    Info,
    InvalidReadyPattern,
    ManagerError,
    Ready,
    ReadyPortInUse,
    StartInput,
)
from .tool_schemas import validate_params

__all__ = [
    "handle_background_process",
    "info_to_dict",
]


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def info_to_dict(info: Info) -> dict[str, Any]:
    """Render an :class:`Info` snapshot as a JSON-serializable dict.

    Uses :func:`dataclasses.asdict` for the deep walk, then post-processes
    the two non-JSON-native fields:

    * ``ports`` is a ``tuple[int, ...]`` on the dataclass; we convert to
      a list so ``json.dumps`` emits a JSON array (tuples vs lists are
      equivalent on the wire, but JSON has only ``array``).
    * ``time`` is the nested :class:`Time` dataclass; :func:`dataclasses.asdict`
      already unpacks it into a plain dict.
    """
    raw = dataclasses.asdict(info)
    # ``asdict`` keeps tuples as tuples; convert to list for JSON.
    raw["ports"] = list(raw.get("ports", ()))
    return raw


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _not_found(process_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "process_not_found",
        "message": f"Background process not found: {process_id}",
    }


def _build_ready(raw: Any) -> Ready | None:
    """Coerce an LLM-shaped ``ready`` mapping into :class:`Ready`.

    Returns ``None`` if ``raw`` is falsy or all sub-fields are absent.
    Type-level validation (port range, timeout > 0, pattern is str) is
    delegated to :class:`Ready.__post_init__`.
    """
    if not raw or not isinstance(raw, dict):
        return None
    pattern = raw.get("pattern")
    port = raw.get("port")
    timeout = raw.get("timeout")
    if pattern is None and port is None and timeout is None:
        return None
    return Ready(pattern=pattern, port=port, timeout=timeout)


async def handle_background_process(
    params: dict,
    *,
    manager: BackgroundProcessManagerPort,
    session_id: str,
) -> dict[str, Any]:
    """Dispatch a ``background_process`` LLM tool invocation.

    Args:
        params: Tool call arguments as decoded from the LLM message.
            Must conform to :data:`BACKGROUND_PROCESS_TOOL_SCHEMA`.
        manager: Live manager instance (typically
            ``container.background_process.manager``).
        session_id: Current LLM session id.  Used as the
            :class:`StartInput.session_id` for ``start`` and as the
            ``list`` filter so the LLM only sees its own session's
            processes.

    Returns:
        JSON-serializable response dict.  Successful responses carry
        ``{"ok": True, ...}``; errors carry ``{"ok": False,
        "error_code": ..., "message": ...}``.

    Raises:
        ValueError: cross-field schema validation failed (missing
            ``command`` on ``start``, missing ``id`` on the id-required
            actions).  Surfaces unchanged so the upstream tool registry
            can format it for the LLM.
    """
    validate_params(params)
    action = params["action"]

    if action == "list":
        infos = await manager.list(session_id=session_id)
        return {
            "ok": True,
            "processes": [info_to_dict(i) for i in infos],
        }

    if action == "start":
        try:
            start_input = StartInput(
                session_id=session_id,
                command=params["command"],
                cwd=params.get("workdir"),
                description=params.get("description"),
                ready=_build_ready(params.get("ready")),
                # 2026-07-13: caller-selected shell alias (LLM-facing).
                # Absent / None keeps the pre-existing auto-select
                # behaviour; the schema validator upstream restricts the
                # allowed set to {"auto", "cmd", "powershell", "sh"}.
                shell=params.get("shell"),
                # ``_inject_file_guard`` is an internal flag set by the LLM
                # tool bridge (``_chat_background_process_tool_bridge``) to
                # opt the spawned child into native FileGuard protection.
                # It is NOT part of the public tool schema — callers that do
                # not set it (App Builder, HTTP operator) get the default
                # False, so their child processes are never subject to the
                # native hook and will not trigger authorization dialogs.
                inject_file_guard=bool(params.get("_inject_file_guard", False)),
            )
            info = await manager.start(start_input)
        except InvalidReadyPattern as e:
            return {
                "ok": False,
                "error_code": "invalid_ready_pattern",
                "message": str(e),
            }
        except ReadyPortInUse as e:
            return {
                "ok": False,
                "error_code": "ready_port_in_use",
                "message": str(e),
            }
        except ManagerError as e:
            return {
                "ok": False,
                "error_code": "manager_error",
                "message": str(e),
            }
        return {"ok": True, "process": info_to_dict(info)}

    # The remaining actions all require an ``id`` and operate by id.
    process_id = params["id"]

    # 2026-07-09 (G) — session ownership check for by-id actions. bgp ids are
    # random, but an LLM must only be able to status/logs/stop/restart a
    # process IT started in THIS conversation. We verify the target's
    # session_id matches the caller's; a mismatch is reported as _not_found
    # (does not leak that the id exists in another session). ``session_id``
    # empty (non-LLM / legacy caller) skips the check — the HTTP operator route
    # and App Builder deliberately operate cross-session (global view).
    def _owned_by_caller(info: object | None) -> bool:
        if info is None:
            return False
        if not session_id:
            return True  # no caller session → no ownership constraint
        return getattr(info, "session_id", None) == session_id

    if action == "status":
        info = await manager.get(process_id)
        if not _owned_by_caller(info):
            return _not_found(process_id)
        return {"ok": True, "process": info_to_dict(info)}

    if action == "logs":
        # ownership first (get is cheap + authoritative on session), then logs.
        owner_info = await manager.get(process_id)
        if not _owned_by_caller(owner_info):
            return _not_found(process_id)
        try:
            logs = await manager.logs(process_id)
        except ManagerError as e:
            return {
                "ok": False,
                "error_code": "manager_error",
                "message": str(e),
            }
        if logs is None:
            return _not_found(process_id)
        return {"ok": True, "id": logs.id, "output": logs.output}

    if action == "stop":
        owner_info = await manager.get(process_id)
        if not _owned_by_caller(owner_info):
            return _not_found(process_id)
        try:
            info = await manager.stop(process_id)
        except ManagerError as e:
            return {
                "ok": False,
                "error_code": "manager_error",
                "message": str(e),
            }
        if info is None:
            return _not_found(process_id)
        return {"ok": True, "process": info_to_dict(info)}

    if action == "restart":
        owner_info = await manager.get(process_id)
        if not _owned_by_caller(owner_info):
            return _not_found(process_id)
        try:
            info = await manager.restart(process_id)
        except InvalidReadyPattern as e:
            return {
                "ok": False,
                "error_code": "invalid_ready_pattern",
                "message": str(e),
            }
        except ReadyPortInUse as e:
            return {
                "ok": False,
                "error_code": "ready_port_in_use",
                "message": str(e),
            }
        except ManagerError as e:
            return {
                "ok": False,
                "error_code": "manager_error",
                "message": str(e),
            }
        if info is None:
            return _not_found(process_id)
        return {"ok": True, "process": info_to_dict(info)}

    # Schema enum should make this unreachable; defensive fallback.
    return {
        "ok": False,
        "error_code": "unknown_action",
        "message": f"Unknown action: {action!r}",
    }
