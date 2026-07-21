# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Typed event envelopes for runner_protocol v3.1.

Each subclass is a frozen dataclass that the host can route on via
``isinstance``. ``payload`` keeps the original JSON dict so payload
fields not yet promoted to attributes are still accessible to consumers
(forward compatibility with future runner_protocol revisions).

The :func:`decode_event` factory is the single entry point — it never
raises on malformed input; instead it falls back to
:class:`StdoutLogEvent` / :class:`UnknownRunnerEvent` so the host's
streaming loop never crashes on a bad line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal, Mapping

__all__ = [
    "LogStream",
    "RunnerEvent",
    "StatusEvent",
    "ProgressEvent",
    "MetricsEvent",
    "LogEvent",
    "ResultEvent",
    "DoneEvent",
    "ErrorEvent",
    "StdoutLogEvent",
    "UnknownRunnerEvent",
    "decode_event",
    "event_kind",
    "is_terminal",
]


LogStream = Literal["stdout", "stderr"]
"""Subset of :attr:`LogEvent.stream` allowed by the SSOT."""


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerEvent:
    """Base type for every runner_protocol v3.1 decoded event.

    Subclasses set their :attr:`type` discriminator; consumers can match
    via ``isinstance`` (preferred) or the discriminator string.

    The :attr:`payload` mapping holds the **original** JSON dict so
    forward-incompatible producers (Pack runners that emit fields the
    host doesn't yet recognise) still surface their data.

    Validation note
    ---------------

    ``__post_init__`` validation is duplicated in subclasses rather than
    delegated through ``super().__post_init__()`` because frozen +
    slotted dataclasses with class hierarchies have known issues with
    ``super()`` (CPython tracker #112433); the duplication is a few
    lines and keeps every subclass self-contained.
    """

    type: ClassVar[str] = "_base"

    payload: Mapping[str, Any] = field(default_factory=dict)
    raw: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)


def _validate_base(self: "RunnerEvent") -> None:
    if not isinstance(self.payload, Mapping):
        raise ValueError(
            "RunnerEvent.payload must be a Mapping, got "
            f"{type(self.payload).__name__}"
        )
    if not isinstance(self.raw, str):
        raise ValueError("RunnerEvent.raw must be str")


# ---------------------------------------------------------------------------
# Subclasses (one per type)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class StatusEvent(RunnerEvent):
    """``{type:"status", state:"preparing|running"}``."""

    type: ClassVar[str] = "status"
    state: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.state, str):
            raise ValueError("StatusEvent.state must be str")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProgressEvent(RunnerEvent):
    """``{type:"progress", phase:"infer", pct:42}``."""

    type: ClassVar[str] = "progress"
    phase: str = ""
    pct: float = 0.0

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.phase, str):
            raise ValueError("ProgressEvent.phase must be str")
        if (
            not isinstance(self.pct, (int, float))
            or isinstance(self.pct, bool)
        ):
            raise ValueError("ProgressEvent.pct must be number")
        # Tolerant range: some runners emit > 100 to indicate "extra"
        # work past the originally-estimated total. Host clamps in the
        # presenter.


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsEvent(RunnerEvent):
    """``{type:"metrics", latencyMs, memoryMB, device, ...}``.

    All metric fields are optional — the runner emits whatever it could
    measure. The host never assumes any subset is present.
    """

    type: ClassVar[str] = "metrics"
    latency_ms: float | None = None
    memory_mb: float | None = None
    device: str | None = None

    def __post_init__(self) -> None:
        _validate_base(self)
        for name, value in (
            ("latency_ms", self.latency_ms),
            ("memory_mb", self.memory_mb),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                raise ValueError(f"MetricsEvent.{name} must be number or None")
        if self.device is not None and not isinstance(self.device, str):
            raise ValueError("MetricsEvent.device must be str or None")


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEvent(RunnerEvent):
    """``{type:"log", stream:"stdout|stderr", line:"..."}``.

    Emitted when a Pack uses the structured log channel (rare —
    ``stderr`` lines arrive as :class:`StdoutLogEvent` instead since
    they bypass the JSON encoder).
    """

    type: ClassVar[str] = "log"
    stream: LogStream = "stdout"
    line: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)
        if self.stream not in ("stdout", "stderr"):
            raise ValueError(
                f"LogEvent.stream must be stdout|stderr, got {self.stream!r}"
            )
        if not isinstance(self.line, str):
            raise ValueError("LogEvent.line must be str")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultEvent(RunnerEvent):
    """``{type:"result", output:{...}}`` — exactly one per run."""

    type: ClassVar[str] = "result"
    output: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.output, Mapping):
            raise ValueError("ResultEvent.output must be a Mapping")


@dataclass(frozen=True, slots=True, kw_only=True)
class DoneEvent(RunnerEvent):
    """``{type:"done"}`` — successful terminal."""

    type: ClassVar[str] = "done"


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(RunnerEvent):
    """``{type:"error", code:..., message:...}`` — failure terminal."""

    type: ClassVar[str] = "error"
    code: str = "UNKNOWN"
    message: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("ErrorEvent.code must be a non-empty str")
        if not isinstance(self.message, str):
            raise ValueError("ErrorEvent.message must be str")


@dataclass(frozen=True, slots=True, kw_only=True)
class StdoutLogEvent(RunnerEvent):
    """Synthetic event for non-JSON / non-protocol stdout lines.

    A Pack that ``print()``s plain text (or whose JSON line is malformed)
    surfaces here so the Logs panel can still render the bytes. The
    host never treats this as a terminal kind.
    """

    type: ClassVar[str] = "stdout_log"
    line: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.line, str):
            raise ValueError("StdoutLogEvent.line must be str")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownRunnerEvent(RunnerEvent):
    """A JSON object with a ``type`` field not in the v3.1 set.

    Forward compatibility: future runner_protocol versions may add new
    kinds; the host doesn't crash, it surfaces them under this envelope
    so a presenter can route them based on ``declared_type``.
    """

    type: ClassVar[str] = "unknown"
    declared_type: str = ""

    def __post_init__(self) -> None:
        _validate_base(self)
        if not isinstance(self.declared_type, str):
            raise ValueError("UnknownRunnerEvent.declared_type must be str")


# ---------------------------------------------------------------------------
# Discriminator constants
# ---------------------------------------------------------------------------
_TYPED_EVENTS: Final[dict[str, type[RunnerEvent]]] = {
    "status": StatusEvent,
    "progress": ProgressEvent,
    "metrics": MetricsEvent,
    "log": LogEvent,
    "result": ResultEvent,
    "done": DoneEvent,
    "error": ErrorEvent,
}
"""Map ``type`` discriminator → concrete subclass."""

_TERMINAL_KINDS: Final[frozenset[str]] = frozenset({"done", "error"})
"""Event kinds that end the run."""


# ---------------------------------------------------------------------------
# Decode / classify
# ---------------------------------------------------------------------------
def decode_event(raw: str) -> RunnerEvent:
    """Parse one stdout line into a :class:`RunnerEvent`.

    * malformed JSON / non-dict → :class:`StdoutLogEvent` carrying the
      raw text;
    * unknown ``type`` → :class:`UnknownRunnerEvent` carrying the dict;
    * known type → corresponding typed subclass.

    Never raises on bad input — the host's per-line loop relies on
    this.
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"decode_event requires str, got {type(raw).__name__}"
        )
    text = raw.rstrip()
    if not text or not text.strip():
        # Whitespace-only — not a meaningful event but the caller may
        # still want a placeholder. We return a zero-payload stdout log
        # so iteration shape stays uniform.
        return StdoutLogEvent(line=text, payload={}, raw=raw)
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        return StdoutLogEvent(line=text, payload={}, raw=raw)
    if not isinstance(obj, dict):
        return StdoutLogEvent(line=text, payload={}, raw=raw)
    declared = obj.get("type")
    if not isinstance(declared, str):
        return UnknownRunnerEvent(
            declared_type="",
            payload=obj,
            raw=raw,
        )
    cls = _TYPED_EVENTS.get(declared)
    if cls is None:
        return UnknownRunnerEvent(
            declared_type=declared,
            payload=obj,
            raw=raw,
        )
    # Build the typed subclass with declared fields where present;
    # missing fields fall back to the dataclass defaults.
    return _build_typed(cls, obj, raw)


def event_kind(event: RunnerEvent) -> str:
    """Return the canonical ``type`` discriminator for ``event``."""
    return event.type


def is_terminal(event: RunnerEvent) -> bool:
    """``True`` iff ``event`` is a terminal (``done`` / ``error``) frame."""
    return event.type in _TERMINAL_KINDS


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _build_typed(
    cls: type[RunnerEvent], obj: dict[str, Any], raw: str
) -> RunnerEvent:
    """Construct a typed subclass from a parsed JSON dict.

    Pulls promoted fields out of ``obj`` (matched by camelCase or
    snake_case key) and falls back to defaults when missing or
    ill-typed. The full ``obj`` is always preserved as :attr:`payload`.
    """
    if cls is StatusEvent:
        return StatusEvent(
            state=_str(obj, "state", default=""),
            payload=obj,
            raw=raw,
        )
    if cls is ProgressEvent:
        pct = obj.get("pct", 0.0)
        return ProgressEvent(
            phase=_str(obj, "phase", default=""),
            pct=float(pct) if isinstance(pct, (int, float)) else 0.0,
            payload=obj,
            raw=raw,
        )
    if cls is MetricsEvent:
        latency = obj.get("latencyMs")
        memory = obj.get("memoryMB")
        return MetricsEvent(
            latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
            memory_mb=float(memory) if isinstance(memory, (int, float)) else None,
            device=_str(obj, "device", default=None),
            payload=obj,
            raw=raw,
        )
    if cls is LogEvent:
        stream = obj.get("stream", "stdout")
        if stream not in ("stdout", "stderr"):
            stream = "stdout"
        return LogEvent(
            stream=stream,  # type: ignore[arg-type]
            line=_str(obj, "line", default=""),
            payload=obj,
            raw=raw,
        )
    if cls is ResultEvent:
        output = obj.get("output", {})
        if not isinstance(output, dict):
            output = {}
        return ResultEvent(output=output, payload=obj, raw=raw)
    if cls is DoneEvent:
        return DoneEvent(payload=obj, raw=raw)
    if cls is ErrorEvent:
        code = _str(obj, "code", default="UNKNOWN")
        if not code:
            code = "UNKNOWN"
        return ErrorEvent(
            code=code,
            message=_str(obj, "message", default=""),
            payload=obj,
            raw=raw,
        )
    # Should be unreachable — _TYPED_EVENTS is the source of truth.
    return UnknownRunnerEvent(  # pragma: no cover — defensive
        declared_type=str(obj.get("type", "")),
        payload=obj,
        raw=raw,
    )


def _str(obj: Mapping[str, Any], key: str, *, default: Any) -> Any:
    value = obj.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        return default
    return value
