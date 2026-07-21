# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-task contextual data (request_id, correlation_id) via ContextVar.

These vars flow across ``await`` boundaries within the same Task by virtue
of how Python's :mod:`contextvars` interacts with :mod:`asyncio`.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qai_request_id", default=None
)
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qai_correlation_id", default=None
)


def current_request_id() -> str | None:
    return _request_id.get()


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def bind_request_id(value: str | None) -> Iterator[None]:
    """Bind ``request_id`` for the duration of the ``with`` block.

    Use as::

        with bind_request_id(req_id):
            await use_case.run()
    """
    token = _request_id.set(value)
    try:
        yield
    finally:
        _request_id.reset(token)


@contextmanager
def bind_correlation_id(value: str | None) -> Iterator[None]:
    """Bind ``correlation_id`` for the duration of the ``with`` block."""
    token = _correlation_id.set(value)
    try:
        yield
    finally:
        _correlation_id.reset(token)


def clear_context() -> None:
    """Reset both context vars to ``None``.

    Intended for test isolation; should not normally be called in
    production code (rely on ``with bind_*`` scoping instead).
    """
    _request_id.set(None)
    _correlation_id.set(None)
