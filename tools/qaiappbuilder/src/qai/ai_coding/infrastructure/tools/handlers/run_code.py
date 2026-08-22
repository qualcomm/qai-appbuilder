# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""``run_code`` tool handler — execute Python in a persistent namespace.

Position relative to ``exec``
-----------------------------
``exec`` runs a program; ``run_code`` runs an expression in a namespace that
outlives the call. Reach for ``run_code`` when the work is computation the
model builds up over several steps (parse a file, then query the parsed
result, then cross-check it) — each step reuses what the previous one bound
instead of re-deriving it. Reach for ``exec`` for anything that is really a
command: builds, tests, package managers, git.

The handler is deliberately thin: it validates arguments, delegates to
:class:`~._code_session.CodeSession`, and renders the outcome. Everything
hard (subprocess lifecycle, hard deadline, crash rebuild, serialisation)
lives in the session so this file stays readable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._code_session import (
    CellOutcome,
    get_code_session,
    resolve_timeout,
)
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    _ok,
    get_tool_output_thresholds,
    resolve_under_workspace,
)
from qai.ai_coding.infrastructure.tools.tool_result_store import (
    ToolResultStorePort,
)

#: Marker prefix for the rendered sections so the model can tell the streams
#: apart at a glance without us inventing a structured render contract.
_STDOUT_HEADER = "--- stdout ---"
_STDERR_HEADER = "--- stderr ---"
_DISPLAY_HEADER = "--- display ---"
_VALUE_HEADER = "--- value ---"
_TRACEBACK_HEADER = "--- traceback ---"


async def tool_run_code(
    args: dict[str, Any],
    *,
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    """Execute ``code`` in the session namespace and render the outcome."""
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ToolError("run_code: 'code' argument is required")

    timeout = resolve_timeout(args.get("timeout"))
    reset = bool(args.get("reset"))
    cwd = _resolve_cwd(args.get("cwd"))

    # M10 parity with the other long-running tools: a cell that takes a while
    # should show that something is happening.
    from qai.platform.tool_progress import emit_progress

    first_line = code.strip().splitlines()[0][:60]
    emit_progress(f"run_code “{first_line}”…", "step")

    session = get_code_session()
    try:
        outcome = await session.run(
            code, timeout=timeout, reset=reset, cwd=cwd
        )
    except RuntimeError as exc:
        # The interpreter could not be started at all — an environment
        # problem, not a problem with this cell.
        raise ToolError(str(exc)) from exc

    return _render(outcome, timeout=timeout, store=tool_result_store)


def _resolve_cwd(raw: Any) -> str | None:
    """Turn a caller-supplied ``cwd`` into an absolute directory, or ``None``.

    A relative path that no workspace base resolved would be interpreted by
    ``create_subprocess_exec`` against the DAEMON's cwd — silently the wrong
    directory. Anchor it explicitly instead, and drop a path that is not a
    directory: passing one makes the spawn fail with a bare OSError, which the
    caller would read as "the interpreter is broken".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    resolved = Path(resolve_under_workspace(raw))
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    if not resolved.is_dir():
        raise ToolError(
            f"run_code: 'cwd' is not an existing directory: {resolved}"
        )
    return str(resolved)


def _render(
    outcome: CellOutcome,
    *,
    timeout: float,
    store: ToolResultStorePort | None,
) -> dict[str, Any]:
    """Turn a :class:`CellOutcome` into the tool result envelope."""
    sections: list[str] = []
    if outcome.displays:
        sections.append(_DISPLAY_HEADER)
        sections.extend(outcome.displays)
    if outcome.stdout:
        sections.append(_STDOUT_HEADER)
        sections.append(outcome.stdout.rstrip("\n"))
    if outcome.stderr:
        sections.append(_STDERR_HEADER)
        sections.append(outcome.stderr.rstrip("\n"))
    if outcome.value is not None:
        sections.append(_VALUE_HEADER)
        sections.append(outcome.value)
    if outcome.traceback:
        sections.append(_TRACEBACK_HEADER)
        sections.append(outcome.traceback)
    body = "\n".join(sections)

    # The shared store owns the whole oversized-output contract: it decides
    # whether the body is too big, renders the head+tail preview, persists the
    # full text and reports the retrieval path. Re-implementing any of that
    # here would drift from what every other tool does.
    stored_path: str | None = None
    truncated = False
    if store is not None and body:
        try:
            outcome_preview = store.store(
                body, tool_name="run_code", context_hint="cell output"
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            outcome_preview = None
        if outcome_preview is not None:
            body = outcome_preview.preview
            stored_path = outcome_preview.stored_path
            # ``truncated`` is the preview's own verdict. ``stored`` only says a
            # file was written, and it is False whenever persistence degraded to
            # preview-only — reporting that as "not truncated" would tell the
            # model the elided body is complete.
            truncated = outcome_preview.truncated
    elif body:
        # No store wired (light stubs / tests): apply a byte ceiling alone so
        # a runaway cell still cannot flood the context window. ``read_max_bytes``
        # is the closest existing budget for a text body of this shape.
        max_bytes = get_tool_output_thresholds().read_max_bytes
        encoded = body.encode("utf-8")
        if len(encoded) > max_bytes:
            body = encoded[:max_bytes].decode("utf-8", errors="replace")
            truncated = True

    message = _summarise(outcome, timeout=timeout)

    result = _ok(
        message,
        output=body,
        status=outcome.status,
        value=outcome.value,
        cell_count=outcome.count,
        namespace_restarted=outcome.restarted,
        truncated=truncated,
    )
    if outcome.error_name:
        result["error_name"] = outcome.error_name
        result["error_message"] = outcome.error_message
    if stored_path is not None:
        result["stored_path"] = stored_path
    return result


def _summarise(outcome: CellOutcome, *, timeout: float) -> str:
    """One-line verdict, plus the recovery hint each failure mode needs."""
    restart_note = ""
    if outcome.restarted:
        # State loss is the one thing the model MUST be told plainly: silently
        # continuing against an empty namespace produces confusing NameErrors
        # several calls later.
        restart_note = (
            " The interpreter was restarted, so every name bound by earlier "
            "calls is GONE — re-create what this cell needs (imports, "
            "variables) before relying on it."
        )

    if outcome.status == "timeout":
        return (
            f"run_code: the cell exceeded its {timeout:.0f}s deadline and the "
            f"interpreter was stopped.{restart_note} Raise `timeout` for a "
            f"genuinely slow computation, or move long-running work to a "
            f"background process."
        )
    if outcome.status == "crashed":
        return (
            f"run_code: the interpreter exited while running this cell (a "
            f"hard crash or an explicit exit).{restart_note}"
        )
    if outcome.status == "error":
        name = outcome.error_name or "Error"
        detail = outcome.error_message or ""
        return f"run_code: the cell raised {name}: {detail}{restart_note}"
    pieces = [f"run_code: cell {outcome.count} ok"]
    if outcome.value is not None:
        pieces.append("value reported")
    if outcome.displays:
        pieces.append(f"{len(outcome.displays)} display(s)")
    return "; ".join(pieces) + restart_note
