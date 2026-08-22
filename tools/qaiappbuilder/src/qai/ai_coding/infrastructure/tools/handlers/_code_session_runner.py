# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Child-side interpreter loop for the ``run_code`` tool.

Runs as ``python -u <this file>`` and speaks newline-delimited JSON over
stdin/stdout. Every statement executes in ONE long-lived namespace, so a
later call can reuse names bound by an earlier one — the property that
makes an interactive kernel worth having over re-running a fresh script.

Wire protocol
-------------
Host → child (one JSON object per line)::

    {"id": "<req>", "code": "<source>", "reset": false}
    {"op": "shutdown"}

Child → host (one JSON object per line)::

    {"type": "ready"}
    {"type": "stdout",  "id": "<req>", "text": "..."}
    {"type": "stderr",  "id": "<req>", "text": "..."}
    {"type": "display", "id": "<req>", "text": "..."}
    {"type": "value",   "id": "<req>", "text": "..."}
    {"type": "error",   "id": "<req>", "name": "...", "message": "...",
     "traceback": "..."}
    {"type": "done",    "id": "<req>", "status": "ok"|"error",
     "count": <int>}

Design notes
------------
* **stdout is captured, never inherited.** The channel carries the
  protocol, so user ``print`` must be redirected or it would corrupt the
  frame stream. Both streams are relayed as ``stdout``/``stderr`` frames.
* **Last-expression value.** A cell ending in an expression reports its
  ``repr`` as a ``value`` frame (mirroring an interactive prompt) without
  the caller having to wrap everything in ``print``.
* **Tracebacks are trimmed.** The frames belonging to this runner are
  dropped so the model sees only its own code's traceback.
* **Errors never kill the loop.** A failing cell reports ``error`` + a
  ``done`` frame and the namespace survives, so the model fixes one step
  and re-runs it instead of rebuilding all prior state.
* **No host imports.** This file must stay importable by a bare
  interpreter: it is executed as a script by the child process and cannot
  depend on the application package being installed.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
import json
import sys
import traceback
from typing import Any

#: Written to stdout once the namespace is ready to accept work.
_READY = {"type": "ready"}

#: Largest text payload carried by ONE stream frame. A bigger write is split
#: across several frames so a single ``print`` of megabytes cannot exceed the
#: host reader's per-frame ceiling (overrunning it loses the frame, and with
#: it the output the cell produced).
_MAX_FRAME_TEXT = 128 * 1024


def _emit(frame: dict[str, Any]) -> None:
    """Write one protocol frame to the real stdout and flush it.

    Uses the ORIGINAL stdout captured at import time: user code may have
    replaced ``sys.stdout`` (or we may be inside a redirect), and the
    protocol must not be affected by anything the cell does.
    """
    line = json.dumps(frame, ensure_ascii=False, default=str)
    _REAL_STDOUT.write(line + "\n")
    _REAL_STDOUT.flush()


# The protocol stream must never fail to encode: text carrying lone
# surrogates (a path read back with ``surrogateescape``, say) would otherwise
# raise ``UnicodeEncodeError`` mid-frame instead of reaching the host.
with contextlib.suppress(AttributeError, OSError, ValueError):
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]

_REAL_STDOUT = sys.stdout
_REAL_STDERR = sys.stderr


class _StreamRelay(io.TextIOBase):
    """File-like object that forwards each write as a protocol frame.

    Line buffering is deliberately absent: a partial line is forwarded as
    soon as it is written so a long-running cell shows progress instead of
    withholding output until a newline arrives.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._req: str | None = None

    def bind(self, req_id: str | None) -> None:
        self._req = req_id

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:  # type: ignore[override]
        if not text:
            return 0
        for start in range(0, len(text), _MAX_FRAME_TEXT):
            _emit(
                {
                    "type": self._kind,
                    "id": self._req,
                    "text": text[start : start + _MAX_FRAME_TEXT],
                }
            )
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing is buffered
        return None


class _Runner:
    """Owns the persistent namespace and executes one cell at a time."""

    def __init__(self) -> None:
        self._namespace: dict[str, Any] = {}
        self._count = 0
        # Request id the currently-executing cell was sent under. ``display``
        # closes over ``self`` and reads it at CALL time, so it must exist
        # before ``_reset_namespace`` builds the prelude below.
        self._current: str | None = None
        self._stdout = _StreamRelay("stdout")
        self._stderr = _StreamRelay("stderr")
        self._reset_namespace()

    # -- namespace ---------------------------------------------------

    def _reset_namespace(self) -> None:
        """Install a fresh namespace carrying the helper prelude."""
        self._namespace = {
            "__name__": "__main__",
            "__builtins__": builtins,
        }
        self._namespace.update(self._prelude())

    def _prelude(self) -> dict[str, Any]:
        """Helpers every cell can use without importing anything.

        Kept intentionally small. Each entry exists because doing it by
        hand in every cell is either verbose (``display``) or easy to get
        wrong on Windows (``read``/``write`` encoding).
        """

        def display(value: Any) -> None:
            """Emit *value* as a display frame (rendered, not returned)."""
            text = value if isinstance(value, str) else repr(value)
            _emit({"type": "display", "id": self._current, "text": text})

        def read_text(path: str, encoding: str = "utf-8") -> str:
            """Read a UTF-8 text file (explicit encoding: Windows-safe)."""
            with open(path, encoding=encoding, errors="replace") as handle:
                return handle.read()

        def write_text(path: str, content: str, encoding: str = "utf-8") -> int:
            """Write text as UTF-8 with LF endings; returns bytes written."""
            with open(path, "w", encoding=encoding, newline="\n") as handle:
                return handle.write(content)

        return {
            "display": display,
            "read_text": read_text,
            "write_text": write_text,
        }

    # -- execution ---------------------------------------------------

    def execute(self, req_id: str, code: str, *, reset: bool) -> None:
        """Run one cell and emit its frames, then a terminal ``done``."""
        self._current = req_id
        if reset:
            self._reset_namespace()
        self._stdout.bind(req_id)
        self._stderr.bind(req_id)

        status = "ok"
        saved_out, saved_err, saved_in = sys.stdout, sys.stderr, sys.stdin
        sys.stdout, sys.stderr = self._stdout, self._stderr
        # stdin carries the REQUEST STREAM. A cell calling ``input()`` would
        # consume the host's next request line and desynchronise the protocol,
        # so hand it an empty stream: ``input()`` raises EOFError instead.
        sys.stdin = io.StringIO()
        try:
            self._run_source(code, req_id)
        except BaseException as exc:  # noqa: BLE001 — report, never propagate
            status = "error"
            self._emit_error(exc, req_id)
        finally:
            sys.stdout, sys.stderr, sys.stdin = saved_out, saved_err, saved_in
            self._count += 1
            _emit(
                {
                    "type": "done",
                    "id": req_id,
                    "status": status,
                    "count": self._count,
                }
            )
            self._current = None

    def _run_source(self, code: str, req_id: str) -> None:
        """Compile and run *code*, reporting a trailing expression's value.

        The body is split so all but a final expression run under ``exec``
        while that expression runs under ``eval`` — this is what lets a
        cell end in ``some_call()`` and have its result reported, matching
        interactive-prompt behaviour.
        """
        parsed = ast.parse(code, filename="<cell>", mode="exec")
        if not parsed.body:
            return
        *head, tail = parsed.body
        if head:
            exec(  # noqa: S102 — executing model-authored code IS the feature
                compile(ast.Module(body=head, type_ignores=[]), "<cell>", "exec"),
                self._namespace,
            )
        if isinstance(tail, ast.Expr):
            value = eval(  # noqa: S307 — same rationale as the exec above
                compile(ast.Expression(body=tail.value), "<cell>", "eval"),
                self._namespace,
            )
            if value is not None:
                _emit({"type": "value", "id": req_id, "text": repr(value)})
        else:
            exec(  # noqa: S102
                compile(ast.Module(body=[tail], type_ignores=[]), "<cell>", "exec"),
                self._namespace,
            )

    def _emit_error(self, exc: BaseException, req_id: str) -> None:
        """Emit an ``error`` frame with a traceback trimmed to user frames."""
        entries = traceback.extract_tb(exc.__traceback__)
        user_frames = [entry for entry in entries if entry.filename == "<cell>"]
        # No ``<cell>`` frame means the failure came from this runner's own
        # compile step (a SyntaxError, say). Falling back to OUR frames would
        # leak paths the model cannot act on, and the formatted exception
        # already names ``<cell>`` and the offending line.
        formatted = "".join(traceback.format_list(user_frames)) + "".join(
            traceback.format_exception_only(type(exc), exc)
        )
        _emit(
            {
                "type": "error",
                "id": req_id,
                "name": type(exc).__name__,
                "message": str(exc),
                "traceback": formatted.rstrip(),
            }
        )


def main() -> int:
    runner = _Runner()
    _emit(_READY)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            # A malformed line cannot be attributed to a request; report it
            # and keep serving rather than exiting (the host may recover).
            _emit(
                {
                    "type": "error",
                    "id": None,
                    "name": "ProtocolError",
                    "message": "malformed request line",
                    "traceback": "",
                }
            )
            continue
        if not isinstance(request, dict):
            continue
        if request.get("op") == "shutdown":
            return 0
        req_id = request.get("id")
        code = request.get("code")
        if not isinstance(req_id, str) or not isinstance(code, str):
            continue
        runner.execute(req_id, code, reset=bool(request.get("reset")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
