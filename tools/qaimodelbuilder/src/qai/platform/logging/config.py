# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Logging configuration: structlog + stdlib bridge.

Output formats:
- ``json`` (default for production) — line-delimited JSON, one record per line.
- ``console`` (default for dev / TTY) — pretty rich-style output, colour if TTY.

Public API is intentionally small: ``configure_logging`` and ``get_logger``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

import structlog
from structlog.types import EventDict, Processor

from qai.platform.time import SystemClock

from .context import current_correlation_id, current_request_id

LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


_clock = SystemClock()


def _add_timestamp(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Inject a tz-aware ISO 8601 timestamp from our Clock abstraction."""
    event_dict["timestamp"] = _clock.now().isoformat()
    return event_dict


def _add_request_context(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Merge ``request_id`` and ``correlation_id`` from contextvars."""
    rid = current_request_id()
    if rid is not None:
        event_dict.setdefault("request_id", rid)
    cid = current_correlation_id()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def _add_log_level(_: Any, method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["level"] = method_name.upper() if method_name != "warn" else "WARNING"
    return event_dict


def configure_logging(
    *,
    level: LogLevel = "INFO",
    fmt: LogFormat | None = None,
    extra_processors: list[Processor] | None = None,
    stream: Any | None = None,
) -> None:
    """Configure structlog + stdlib logging.

    Idempotent: safe to call from multiple entry points (test fixtures,
    ``apps/api/main.py``). Each call REPLACES previous configuration.

    Parameters:
        level: minimum log level for both stdlib and structlog.
        fmt: ``"json"`` for line-delimited JSON, ``"console"`` for human
            readable. ``None`` auto-selects: ``"console"`` if stderr is a
            TTY, otherwise ``"json"``.
        extra_processors: optional processors inserted just before the
            renderer (e.g. an audit-redaction processor).
        stream: optional output stream override (used by tests).
            Defaults to ``sys.stderr`` resolved at call time.
    """
    resolved_stream = stream if stream is not None else sys.stderr
    resolved_fmt: LogFormat = fmt if fmt is not None else _auto_fmt(resolved_stream)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_request_context,
        _add_log_level,
        _add_timestamp,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]
    if extra_processors:
        shared_processors.extend(extra_processors)

    renderer: Processor
    if resolved_fmt == "json":
        # ``format_exc_info`` turns the ``exc_info`` tuple into a string field
        # for the JSON renderer.  It MUST NOT be in the chain when the
        # ConsoleRenderer is used: that renderer does its own pretty-exception
        # rendering and structlog warns ("Remove ``format_exc_info`` from your
        # processor chain if you want pretty exceptions") + prints exceptions
        # poorly when both are active.  So we add it ONLY on the JSON branch.
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
        shared_processors.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=getattr(resolved_stream, "isatty", lambda: False)(),
            sort_keys=True,
        )

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=resolved_stream),
        cache_logger_on_first_use=False,
    )

    # Bridge stdlib logging through to the same handler so that third-party
    # libraries (uvicorn, sqlalchemy, httpx) emit through the same pipeline.
    root = logging.getLogger()
    # Remove handlers added by previous configure_logging calls (idempotency).
    for handler in list(root.handlers):
        if getattr(handler, "_qai_managed", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(resolved_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._qai_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger.

    Use module ``__name__`` as the logger name in production code.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def _auto_fmt(stream: Any) -> LogFormat:
    return "console" if getattr(stream, "isatty", lambda: False)() else "json"
