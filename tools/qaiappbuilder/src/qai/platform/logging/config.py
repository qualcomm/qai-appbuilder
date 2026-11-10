# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Logging configuration: structlog + stdlib bridge.

Output formats:
- ``json`` (default for production) — line-delimited JSON, one record per line.
- ``console`` (default for dev / TTY) — pretty rich-style output, colour if TTY.

Optionally a size-rotated log FILE is written as well (``file_path``), so a
run can be inspected afterwards even when nobody watched the console.

Public API is intentionally small: ``configure_logging`` and ``get_logger``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any, Literal

import structlog
from structlog.types import EventDict, Processor

from qai.platform.time import SystemClock

from .context import current_correlation_id, current_request_id

LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


_clock = SystemClock()


def _add_timestamp(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Inject an ISO 8601 timestamp in the local timezone.

    The Clock abstraction yields a tz-aware UTC instant (S1 PR-012 keeps the
    domain UTC-only). For human-readable logs we render it in the machine's
    local timezone via ``astimezone()`` (no argument = system local zone) so
    operators read wall-clock time. The UTC offset (e.g. ``+08:00``) is kept,
    so the timestamp stays unambiguous and machine-parseable.
    """
    event_dict["timestamp"] = _clock.now().astimezone().isoformat(timespec="microseconds")
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
    file_path: Path | None = None,
    file_max_bytes: int = 10 * 1024 * 1024,
    file_backup_count: int = 5,
    file_fmt: LogFormat = "console",
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
        file_path: also write every record to this file (UTF-8, appended,
            rotated at ``file_max_bytes`` keeping ``file_backup_count`` old
            files ``<name>.1`` .. ``<name>.N``). ``None`` = stderr only. A file
            that cannot be opened is reported on stderr and skipped; logging
            to stderr keeps working.
        file_fmt: ``"console"`` (plain text, no colours) or ``"json"``.
    """
    resolved_stream = stream if stream is not None else sys.stderr
    resolved_fmt: LogFormat = fmt if fmt is not None else _auto_fmt(resolved_stream)

    # ``shared_processors`` run for BOTH structlog-native records (as the
    # front of the structlog chain) and foreign stdlib records (as the
    # ProcessorFormatter ``foreign_pre_chain``), so uvicorn / httpx / worker /
    # migrate logs get the SAME local-tz ISO timestamp + level as our own.
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


    # structlog-native records are handed off to the stdlib ``logging`` root
    # handler (via ``wrap_for_formatter``); the ProcessorFormatter installed
    # below then renders them — the SAME formatter used for foreign records,
    # so there is exactly one output pipeline and one format.
    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    formatter = _make_formatter(
        shared_processors,
        resolved_fmt,
        colors=getattr(resolved_stream, "isatty", lambda: False)(),
    )

    # Single root handler renders EVERY record — structlog-native and foreign
    # stdlib (uvicorn, sqlalchemy, httpx, sticky worker, aria2c, ...) — through
    # the one ProcessorFormatter above.
    root = logging.getLogger()
    # Remove handlers added by previous configure_logging calls (idempotency).
    for handler in list(root.handlers):
        if getattr(handler, "_qai_managed", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(resolved_stream)
    handler.setFormatter(formatter)
    handler._qai_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if file_path is not None:
        file_handler = _open_file_handler(
            Path(file_path), max_bytes=file_max_bytes, backup_count=file_backup_count
        )
        if file_handler is not None:
            file_handler.setFormatter(
                _make_formatter(shared_processors, file_fmt, colors=False)
            )
            file_handler._qai_managed = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
    root.setLevel(level)


def _make_formatter(
    shared_processors: list[Processor], fmt: LogFormat, *, colors: bool
) -> structlog.stdlib.ProcessorFormatter:
    # Processors that run inside the ProcessorFormatter, AFTER the shared /
    # foreign_pre_chain, on every record just before rendering.
    processors: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if fmt == "json":
        # ``format_exc_info`` turns the ``exc_info`` tuple into a string field
        # for the JSON renderer. It MUST NOT be used with ConsoleRenderer,
        # which does its own pretty-exception rendering.
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer(sort_keys=True))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=colors, sort_keys=True))
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=processors,
    )


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that survives a failed rollover.

    On Windows the rename fails while another program holds the file open
    (an editor, a second backend process). The stock handler then reports an
    error on EVERY record and retries the rename each time. Here a failed
    rename is swallowed, writing goes on into the current file, and the next
    attempt waits ``_RETRY_SECONDS``.
    """

    _RETRY_SECONDS = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._next_rollover_try = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802 (stdlib name)
        if time.monotonic() < self._next_rollover_try:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        try:
            super().doRollover()
        except OSError:
            self._next_rollover_try = time.monotonic() + self._RETRY_SECONDS
            if self.stream is None:  # the base closed it before renaming
                self.stream = self._open()  # mode "a": keep appending


def open_rotating_file_handler(
    path: Path, *, max_bytes: int, backup_count: int
) -> logging.Handler | None:
    """Size-rotated UTF-8 append handler that survives a locked rollover.

    Returns ``None`` (after a note on stderr) when the file cannot be opened.
    Public so other components can keep their own log file (e.g. the
    GenieAPIService output in ``data/logs/genie_service.log``).
    """
    return _open_file_handler(Path(path), max_bytes=max_bytes, backup_count=backup_count)


def _open_file_handler(
    path: Path, *, max_bytes: int, backup_count: int
) -> logging.Handler | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return _SafeRotatingFileHandler(
            path,
            maxBytes=max(0, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        print(f"[qai.logging] cannot open log file {path}: {exc}", file=sys.stderr)
        return None


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger.

    Use module ``__name__`` as the logger name in production code.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def _auto_fmt(stream: Any) -> LogFormat:
    return "console" if getattr(stream, "isatty", lambda: False)() else "json"
