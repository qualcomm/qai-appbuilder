# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SubAgentError`` — the single typed failure shape of a sub-agent run.

Every way a spawned sub-agent can fail collapses into ONE domain type
carrying a machine-readable :class:`SubAgentErrorKind` plus a
human/LLM-readable message.  The spawn path (preflight → isolation →
execution) previously surfaced failures as bare ``RuntimeError`` /
free-form strings, which meant the caller could neither branch on the
failure class (is this retryable? is it the user's fault?) nor render a
consistent tool-result body back to the model.

:meth:`SubAgentError.render_for_llm` is the ONE rendering used when the
failure is fed back to the parent agent as a tool result, so the model
sees the same shape regardless of which stage failed.

Purity
------
Domain layer: standard library only, and **no runtime side effects** —
no logging, no IO, no clock reads.  In particular
:meth:`render_for_llm` deliberately does not log: the
``subagent.error.rendered`` observability key belongs to the adapter that
calls this method (the spawn/tool boundary), not to the value object.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["SubAgentError", "SubAgentErrorKind"]


class SubAgentErrorKind(str, Enum):
    """Which stage / cause a sub-agent failure belongs to.

    * ``PREFLIGHT`` — rejected before any work started (unknown agent
      type, spawn depth exceeded, spawn not granted, bad request shape).
    * ``ISOLATION`` — the sub-agent's isolated workspace / session could
      not be established or was lost.
    * ``EXECUTION`` — the sub-agent ran and failed (model error, tool
      blew up, malformed final answer).
    * ``BUDGET`` — stopped by a resource cap (token budget, round cap,
      concurrency/queue limits).
    * ``INTERRUPTED`` — aborted by the user or by parent-turn teardown;
      not a defect.
    """

    PREFLIGHT = "preflight"
    ISOLATION = "isolation"
    EXECUTION = "execution"
    BUDGET = "budget"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SubAgentError(Exception):
    """An immutable, raisable sub-agent failure.

    ``frozen=True, slots=True`` on an :class:`Exception` subclass is valid
    on the supported interpreter (CPython 3.12): ``BaseException`` stores
    ``args`` in a C-level struct member rather than a ``__slots__`` entry,
    so the generated ``__slots__`` does not collide and ``frozen``
    assignment guards still fire for the declared fields.

    Two small pieces of glue are needed because the dataclass-generated
    ``__init__`` replaces ``BaseException.__init__``:

    * :meth:`__post_init__` re-seeds ``args`` so ``str(exc)`` and
      traceback rendering show the message instead of an empty string;
    * :meth:`__reduce__` restores ``copy`` / ``pickle`` support, which
      the keyword-only generated ``__init__`` would otherwise break when
      the instance travels across a process or job boundary.
    """

    kind: SubAgentErrorKind
    message: str
    subagent_id: str | None = None
    recovery_hint: str | None = None

    def __post_init__(self) -> None:
        # ``args`` lives in BaseException's C struct, not in the dataclass
        # fields, so this write is unaffected by ``frozen=True``.
        Exception.__init__(self, self.message)

    def __reduce__(
        self,
    ) -> tuple[type[SubAgentError], tuple[object, ...]]:
        return (
            type(self),
            (self.kind, self.message, self.subagent_id, self.recovery_hint),
        )

    def render_for_llm(self) -> str:
        """Render the failure as the tool-result body the parent agent reads.

        ``[sub-agent {kind}: {message}]``, with the recovery hint on a
        second line when present.  No trailing newline when there is no
        hint — the parent agent's transcript should not gain blank lines
        for failures that carry no actionable advice.
        """
        head = f"[sub-agent {self.kind.value}: {self.message}]"
        if self.recovery_hint is None:
            return head
        return f"{head}\n{self.recovery_hint}"
