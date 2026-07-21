# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Cross-process audit bridge for child-process protected-path denies (P-08 #6).

The child-process audit sentinel (``qai.platform.child_process_audit_sentinel``'s
``sitecustomize``) runs inside an ISOLATED child interpreter: stdlib-only, no
``qai.*`` import, no parent DB / event loop. When it denies a write into a
protected prefix it raises ``PermissionError`` from the hooked call site — but
it has no way to reach the parent's ``security_audit_entry`` funnel. Historically
that deny was recorded NOWHERE (an observability gap, symmetric to the P-17 §6.3
gap the MAIN-process sentinel closed).

**Wire protocol (stderr marker line, approved design A).** Before raising, the
child sentinel writes ONE structured marker line to ``sys.stderr`` (best-effort,
zero file IO — the only channel that reliably crosses the process boundary and
is already captured by the ``exec`` handler):

    ``\n[[QAI_PROTECTED_DENY]] {"op": "write", "path": "...", "prefix": "..."}\n``

The PARENT (``exec`` tool handler, ``qai.ai_coding``) captures the child's
stderr. This module supplies the two halves the parent needs WITHOUT crossing a
layer boundary:

  * :func:`parse_and_strip_deny_markers` — extract every marker line from the
    captured stderr, return the parsed denies AND the stderr with those lines
    REMOVED (so the marker never pollutes what is handed back to the user /
    model), and
  * :func:`notify_child_protected_deny` — dispatch each parsed deny to the
    optional, injected sync callback.

**Layering (why this lives in ``qai.platform``).** ``exec.py`` is in
``qai.ai_coding`` and import-linter FORBIDS it from importing ``apps`` (where
:class:`AuditBypassSink` lives). ``qai.platform`` is the shared kernel every
context may import (``qai.** -> qai.platform.**`` is the sole allowed edge), so
both ``qai.ai_coding`` (the parser + notifier consumer) and ``apps`` (the sink
wirer) depend only on THIS module — never on each other. The security coupling
is injected on the apps side via :func:`set_on_child_protected_deny`, exactly
mirroring ``main_process_audit_sentinel._ON_VIOLATION``: a pure-stdlib callback
signature keeps this module security-agnostic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

__all__ = [
    "MARKER_PREFIX",
    "ChildProtectedDeny",
    "format_deny_marker",
    "notify_child_protected_deny",
    "parse_and_strip_deny_markers",
    "set_on_child_protected_deny",
]

#: The line-leading token the child sentinel emits before each deny. Chosen to
#: be unmistakable in captured stderr and cheap to regex out. Kept in sync with
#: ``child_process_audit_sentinel/sitecustomize.py`` (which — being stdlib-only
#: and unable to import qai — hard-codes the SAME literal; see the note there).
MARKER_PREFIX = "[[QAI_PROTECTED_DENY]]"

#: Matches a full marker line (leading whitespace tolerated) and captures the
#: trailing JSON payload. ``re.MULTILINE`` so ``^``/``$`` anchor per physical
#: line; the whole line (including its trailing newline, if any) is stripped.
_MARKER_LINE_RE = re.compile(
    r"^[ \t]*" + re.escape(MARKER_PREFIX) + r"[ \t]*(?P<json>\{.*?\})[ \t]*\r?\n?",
    re.MULTILINE,
)


class ChildProtectedDeny:
    """One parsed child-process protected-path deny (stdlib-typed, immutable-ish).

    Attributes mirror the marker JSON: ``op`` (write / delete / …), ``path``
    (the target the child tried to write) and ``prefix`` (the protected prefix
    it matched). ``op`` defaults to ``"write"`` when the child omitted it.
    """

    __slots__ = ("op", "path", "prefix")

    # Value-equality only (compared in tests); instances are never used as
    # dict keys / set members, so no hashing is needed.
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, op: str, path: str, prefix: str = "") -> None:
        self.op = op or "write"
        self.path = path
        self.prefix = prefix

    def __repr__(self) -> str:  # pragma: no cover — debug aid only
        return (
            f"ChildProtectedDeny(op={self.op!r}, path={self.path!r}, "
            f"prefix={self.prefix!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ChildProtectedDeny):
            return NotImplemented
        return (
            self.op == other.op
            and self.path == other.path
            and self.prefix == other.prefix
        )


# P-08 #6 — optional, best-effort SYNC callback wired by the apps layer via
# :func:`set_on_child_protected_deny`. Invoked once per parsed child deny with
# ``(op: str, path: str)``. CONTRACT (mirrors main_process_audit_sentinel):
#
#   * Pure-stdlib signature (``Callable[[str, str], None]``) — this module lives
#     in ``qai.platform`` and import-linter FORBIDS importing ``qai.security`` /
#     ``apps``; the callback carries the security coupling on the apps side.
#   * BEST-EFFORT: a raising callback is swallowed. The parent has ALREADY
#     stripped the marker and the child has ALREADY raised PermissionError, so
#     the deny (interception) semantic never depends on this succeeding.
#
# ``None`` (default) keeps the historical behaviour: parsing still strips the
# marker line, but no audit row is emitted.
_ON_CHILD_DENY: Callable[[str, str], None] | None = None


def set_on_child_protected_deny(
    callback: Callable[[str, str], None] | None,
) -> None:
    """Register (or clear with ``None``) the child-deny audit callback.

    Wired once by the apps layer (lifespan) to :meth:`AuditBypassSink.enqueue`
    (bound with the child source/subject). Idempotent; a later call replaces the
    previously wired callback. The signature is deliberately pure-stdlib so this
    ``qai.platform`` module never imports ``qai.security`` / ``apps``.
    """
    global _ON_CHILD_DENY  # noqa: PLW0603 — module singleton wiring point
    _ON_CHILD_DENY = callback


def format_deny_marker(op: str, path: str, prefix: str) -> str:
    """Build the exact stderr marker line the child sentinel should emit.

    Provided for parity/testing so the parent-side parser and the child-side
    emitter cannot drift. The child (stdlib-only) hard-codes the same shape; a
    round-trip test asserts :func:`parse_and_strip_deny_markers` recovers what
    this produces. Returns a line WITH leading + trailing ``\\n`` so it is always
    on its own physical line regardless of surrounding child output.
    """
    payload = json.dumps(
        {"op": op or "write", "path": path, "prefix": prefix},
        ensure_ascii=True,
    )
    return f"\n{MARKER_PREFIX} {payload}\n"


def parse_and_strip_deny_markers(
    err_text: str,
) -> tuple[str, list[ChildProtectedDeny]]:
    """Extract + REMOVE every child-deny marker line from captured stderr.

    Returns ``(clean_err_text, denies)``:

      * ``clean_err_text`` — ``err_text`` with all marker lines removed, so the
        stderr handed back to the user / model is never polluted by the internal
        protocol. When there are no markers the ORIGINAL string is returned
        unchanged (no accidental normalisation of newlines etc.).
      * ``denies`` — one :class:`ChildProtectedDeny` per recovered marker (a
        marker whose JSON is malformed is dropped from the list but its line is
        STILL stripped, so a corrupt marker never reaches the user either).

    Pure / no IO / never raises.
    """
    if not err_text or MARKER_PREFIX not in err_text:
        return err_text, []

    denies: list[ChildProtectedDeny] = []

    def _consume(match: re.Match[str]) -> str:
        raw = match.group("json")
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                denies.append(
                    ChildProtectedDeny(
                        op=str(obj.get("op", "write")),
                        path=str(obj.get("path", "")),
                        prefix=str(obj.get("prefix", "")),
                    )
                )
        except Exception:  # noqa: BLE001,S110 — malformed marker: drop, still strip
            pass
        return ""

    clean = _MARKER_LINE_RE.sub(_consume, err_text)
    return clean, denies


def notify_child_protected_deny(denies: list[ChildProtectedDeny]) -> None:
    """Dispatch each parsed child deny to the wired callback (best-effort).

    No-op when no callback is wired (inert / test container). A callback that
    raises is swallowed per deny so one bad row never blocks the others and the
    caller's control flow (returning the exec result) is never affected. Never
    raises.
    """
    cb = _ON_CHILD_DENY
    if cb is None or not denies:
        return
    for deny in denies:
        try:  # noqa: SIM105 — explicit swallow (no contextlib dep in hot path)
            cb(deny.op, deny.path)
        except Exception:  # noqa: BLE001,S110 — audit best-effort; never break exec
            pass
