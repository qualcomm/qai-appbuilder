# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared JSONL audit-sink factory (apps-layer, P-17 §6.3).

Both the FileGuard fail-closed audit (#7 ``emergency_audit.jsonl``) and the
FileBroker deny audit (#1 ``file_broker_audit.jsonl``) previously hand-rolled
their own ``json.dumps`` + ``open(...,"a")`` append blocks with slightly
DIFFERENT key sets (the emergency schema had ``caller``/``mode``; the
file_broker one did not). That drift is exactly the kind of scattered,
inconsistent audit landing site P-17 unifies.

This factory produces a single, best-effort JSONL append sink with one
canonical line schema, so both landing sites emit the SAME shape (unused keys
are simply omitted by the caller passing ``""``/``None``). The write is
best-effort: any failure is swallowed (audit must never break a tool call or
a fail-closed deny path).

The file IO lives here in the apps layer — the security context's
:class:`~qai.security.application.security_audit_facade.SecurityAuditFacade`
stays IO-free and receives one of these callables as an injected JSONL sink.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["make_jsonl_audit_sink"]


def make_jsonl_audit_sink(
    path: "Path | None", *, source: str
) -> Callable[[dict], None]:
    """Return a best-effort ``(record: dict) -> None`` JSONL append sink.

    ``path`` is the target JSONL file (parent dirs created on first write);
    when ``None`` the sink is a no-op (audit disabled — e.g. no data dir).
    ``source`` labels the landing site (``"emergency"`` / ``"file_broker"``)
    and is stamped onto every line unless the record overrides it.

    Canonical line schema (superset; a caller omits keys it lacks):
    ``ts, op, path, decision, caller, source, reason, mode``.
    """

    def _sink(record: dict) -> None:
        if path is None:
            return
        try:
            ts = record.get("ts") or (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z"
            )
            line = {
                "ts": ts,
                "op": record.get("op", ""),
                "path": str(record.get("path", "")),
                "decision": record.get("decision", "deny"),
                "caller": record.get("caller", ""),
                "source": record.get("source") or source,
                "reason": record.get("reason", ""),
                "mode": record.get("mode", ""),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — audit is best-effort, never raises
            pass

    return _sink
