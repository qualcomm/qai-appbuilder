# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Worker subprocess entry point for desktop control.

Launched as ``python -m qai.platform.computer.worker_entry``. Reads
NDJSON request frames from ``stdin`` and writes NDJSON response frames to
``stdout``. All Win32 work happens on a single dedicated thread so
capture and input are strictly serialized and never re-enter (book 03
§2 / §5.2).

Protocol (book 03 §3): ping->pong, init->ready, execute->result,
close->closed; failures become ``error`` frames (with ``id`` for a batch
failure, without for init/fatal failures).
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Any

from . import _protocol as proto
from .session import DesktopSession, Win32Backend
from .types import DesktopError, SessionOptions

__all__ = ["main", "run_loop"]


class _WorkerThread:
    """Owns the DesktopSession on one dedicated OS thread.

    The protocol loop (main thread) hands work items to this thread via a
    queue and blocks on a per-item reply so Win32 calls stay single
    threaded, matching the thread-affinity requirement.
    """

    def __init__(self) -> None:
        self._in: "queue.Queue[tuple[str, Any] | None]" = queue.Queue()
        self._out: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._session: DesktopSession | None = None
        self._thread = threading.Thread(
            target=self._run, name="desktop-worker", daemon=True
        )
        self._thread.start()

    def call(self, op: str, payload: Any) -> tuple[str, Any]:
        """Submit an op to the worker thread and wait for its reply."""
        self._in.put((op, payload))
        return self._out.get()

    def shutdown(self) -> None:
        self._in.put(None)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            item = self._in.get()
            if item is None:
                if self._session is not None:
                    try:
                        self._session.close()
                    except Exception:  # noqa: BLE001
                        pass
                return
            op, payload = item
            try:
                self._out.put(("ok", self._dispatch(op, payload)))
            except DesktopError as exc:
                self._out.put(("err", exc))
            except Exception as exc:  # noqa: BLE001
                self._out.put(("err", DesktopError(str(exc), name="WorkerError")))

    def _dispatch(self, op: str, payload: Any) -> Any:
        if op == "init":
            options = SessionOptions.from_dict(payload)
            self._session = DesktopSession(options, backend=Win32Backend())
            return self._session.capabilities
        if op == "caps":
            if self._session is None:
                raise DesktopError("session not initialised", name="WorkerError")
            return self._session.capabilities
        if op == "execute":
            if self._session is None:
                raise DesktopError("session not initialised", name="WorkerError")
            actions = [proto.action_from_wire(a) for a in payload]
            return self._session.execute(actions)
        raise DesktopError(f"unknown op: {op}", name="WorkerError")


def run_loop(stdin: Any, stdout: Any, *, worker: _WorkerThread | None = None) -> None:
    """Read/dispatch/write the protocol loop until ``close`` or EOF.

    ``stdin`` yields lines (text); ``stdout`` has ``.write``/``.flush``.
    ``worker`` is injectable for tests.
    """
    wk = worker if worker is not None else _WorkerThread()

    def emit(frame: dict[str, Any]) -> None:
        stdout.write(proto.encode_frame(frame).decode("utf-8"))
        stdout.flush()

    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break  # EOF
            line = raw.strip()
            if not line:
                continue
            try:
                frame = proto.decode_frame(line)
            except Exception:  # noqa: BLE001 — any malformed frame
                emit(proto.make_error(frame_id=None, name="ProtocolError", message="bad frame"))
                continue
            if frame.type == "ping":
                emit(proto.make_pong(int(frame.payload.get("id", 0))))
                continue
            if frame.type == "close":
                emit(proto.make_closed())
                break
            if frame.type == "init":
                status, value = wk.call("init", frame.payload.get("options") or {})
                if status == "ok":
                    emit(proto.make_ready(value))
                else:
                    emit(proto.make_error(frame_id=None, name=value.name, message=value.message))
                continue
            if frame.type == "execute":
                fid = int(frame.payload.get("id", 0))
                status, value = wk.call("execute", frame.payload.get("actions") or [])
                if status == "ok":
                    caps_status, caps_value = wk.call("caps", None)
                    if caps_status != "ok":
                        emit(proto.make_error(frame_id=fid, name=caps_value.name, message=caps_value.message))
                        continue
                    emit(proto.make_result(fid, value, caps_value))
                else:
                    emit(proto.make_error(frame_id=fid, name=value.name, message=value.message))
                continue
            emit(proto.make_error(frame_id=None, name="ProtocolError", message=f"unknown frame {frame.type}"))
    finally:
        wk.shutdown()


def main() -> int:
    run_loop(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
