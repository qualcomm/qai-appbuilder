# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared, bind-based TCP port allocation for local services.

This module is the single project-level source of truth for "which port
can this host *really* bind right now". It exists because two independent
consumers need identical semantics and must not drift:

* ``apps/cli/serve.py`` -- the daemon supervisor picking the API port
  (was the original home of ``_can_bind`` / ``_resolve_bindable_port``).
* ``qai.app_builder`` -- the standalone-fullstack-app run manager, which
  must allocate a port for each spawned FastAPI subprocess.

State-Truth-First (AGENTS.md iron-rule 5.1): the only authoritative
answer to "is this port usable" is the OS itself via a real
``socket.bind()`` attempt. Parsing ``netsh excludedportrange`` or relying
on a bare ``connect`` probe would be a brittle proxy -- ``bind()`` is the
truth. On Windows we set ``SO_EXCLUSIVEADDRUSE`` so the probe mirrors
uvicorn's own bind semantics and does not falsely succeed against a
``SO_REUSEADDR`` binder.

Note on TOCTOU: there IS a window between probe and the child's later
bind. In practice the interval is sub-millisecond, but callers that spawn
a subprocess should still treat a post-spawn bind failure as "retry with
the next candidate" -- ``resolve_bindable_port`` alone cannot close the
window.
"""

from __future__ import annotations

import contextlib
import socket
import sys
from collections.abc import Callable, Sequence

__all__ = [
    "DEFAULT_FALLBACK_PORTS",
    "NoBindablePortError",
    "PortAllocationError",
    "PortInUseError",
    "can_bind",
    "resolve_bindable_port",
]


#: Highest valid TCP port number (inclusive).
_MAX_PORT = 65535


# A broad default candidate range for auxiliary local services (e.g. App
# Builder app previews). Deliberately distinct from the daemon's own
# ``apps/cli/serve.py:FALLBACK_PORTS`` so the two do not contend for the
# same first pick. High, uncommon ports reduce collision odds.
DEFAULT_FALLBACK_PORTS: tuple[int, ...] = (
    18420,
    18421,
    18422,
    18423,
    18424,
    18425,
    18426,
    18427,
    18428,
    18429,
)


class PortAllocationError(Exception):
    """Base for all port-allocation failures raised by this module."""


class PortInUseError(PortAllocationError):
    """A *requested* (explicit) port could not be bound."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"port {port} cannot be bound on {host} (another process is "
            "listening, or the port falls inside a Windows reserved range "
            "-- `netsh int ipv4 show excludedportrange protocol=tcp`)."
        )


class NoBindablePortError(PortAllocationError):
    """No candidate in the fallback range could be bound."""

    def __init__(self, host: str, tried: Sequence[int]) -> None:
        self.host = host
        self.tried = tuple(tried)
        super().__init__(
            f"no bindable port found in candidate list {list(tried)} on "
            f"{host}. All candidates failed bind() -- likely all occupied "
            "or inside a Windows reserved range."
        )


def can_bind(host: str, port: int) -> bool:
    """Return ``True`` iff a fresh TCP socket can ``bind((host, port))``.

    Detects ports that are *truly* unusable on the current OS -- covering
    both ordinary "already in use" (``EADDRINUSE`` / ``WinError 10048``)
    and the Windows "reserved by the OS" case (``WinError 10013`` /
    ``EACCES``) where ``netstat`` shows nobody listening but a Hyper-V /
    WSL2 / WinNAT excluded-port-range still rejects the bind.

    The socket is closed immediately after probing. On Windows we set
    ``SO_EXCLUSIVEADDRUSE`` so the probe does not silently succeed against
    a port another process bound with ``SO_REUSEADDR``.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            with contextlib.suppress(AttributeError, OSError):  # pragma: no cover
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,  # type: ignore[attr-defined]
                    1,
                )
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):  # pragma: no cover
            sock.close()


def resolve_bindable_port(
    host: str = "127.0.0.1",
    *,
    requested: int | None = None,
    fallbacks: Sequence[int] = DEFAULT_FALLBACK_PORTS,
    can_bind_fn: Callable[[str, int], bool] = can_bind,
) -> int:
    """Pick a port that ``host`` can really bind right now.

    Priority:

    * If ``requested`` is not ``None``, only that port is tried. If it
      cannot bind, :class:`PortInUseError` is raised -- we honour the
      explicit intent rather than silently substituting another port.
    * Otherwise probe ``fallbacks`` in order and return the first port
      that ``bind()`` accepts.
    * If no candidate binds, :class:`NoBindablePortError` is raised.

    Tests inject ``can_bind_fn`` to simulate excluded / occupied ports
    without touching real sockets.
    """

    if requested is not None:
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise TypeError(
                f"requested must be int or None, got {type(requested).__name__}"
            )
        if not (0 < requested <= _MAX_PORT):
            raise ValueError(f"requested must be in 1..65535, got {requested}")
        if can_bind_fn(host, requested):
            return requested
        raise PortInUseError(host, requested)

    tried: list[int] = []
    seen: set[int] = set()
    for port in fallbacks:
        if port in seen:
            continue
        seen.add(port)
        if can_bind_fn(host, port):
            return port
        tried.append(port)
    raise NoBindablePortError(host, tried)
