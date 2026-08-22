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
    "describe_port_holder",
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
    hard: bool = True,
) -> int:
    """Pick a port that ``host`` can really bind right now.

    Priority:

    * If ``requested`` is not ``None`` and ``hard`` is ``True`` (default),
      only that port is tried. If it cannot bind,
      :class:`PortInUseError` is raised -- we honour the explicit intent
      rather than silently substituting another port.
    * If ``requested`` is not ``None`` and ``hard`` is ``False``, the
      requested port is TRIED FIRST but a bind failure quietly falls
      through to ``fallbacks`` (soft-preferred: an ``app.yaml``
      ``preferred_port`` should be honoured when free, without leaving
      the user stranded when a stale process still holds it).
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
        if hard:
            raise PortInUseError(host, requested)
        # Soft mode: fall through to the fallback list. Exclude the
        # already-failed ``requested`` port from the candidates so the error
        # message on total exhaustion accurately reflects what was tried.
        fallbacks = tuple(p for p in fallbacks if p != requested)

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


def describe_port_holder(port: int) -> dict[str, str | int] | None:
    """Best-effort ``(pid, name)`` of whoever is holding ``port`` LOCALLY.

    Returns a dict with ``pid`` (int) and, when we can resolve it, ``name``
    (executable / image name). Returns ``None`` when the OS lookup fails,
    yields nothing, or is not implemented for this platform — the caller
    MUST treat this as a UX hint, never as a hard fact (a port that just
    fell into ``TIME_WAIT`` has no owning process to name).

    Windows path uses ``netstat -ano`` + ``tasklist /FI "PID eq <pid>"``;
    both are shipped with every Windows install and need no elevation.
    POSIX is not implemented (yet) — the release target is ARM64 Windows.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    if not (0 < port <= _MAX_PORT):
        return None
    if sys.platform != "win32":
        return None
    try:
        import subprocess

        # netstat -ano produces lines like:
        #   TCP    127.0.0.1:1979         0.0.0.0:0    LISTENING    12345
        # Match ONLY LISTENING rows on our exact port so we don't grab the
        # remote-side ephemeral peer of an established connection.
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — no console flash
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    pid: int | None = None
    needle = f":{port} "
    for line in completed.stdout.splitlines():
        if needle not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        break
    if pid is None:
        return None
    holder: dict[str, str | int] = {"pid": pid}
    try:
        tl = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=0x08000000,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return holder
    # tasklist CSV row: "image.exe","<pid>","Services","0","1,234 K"
    first_line = tl.stdout.splitlines()[0] if tl.stdout else ""
    if first_line.startswith('"'):
        first_field = first_line.split(",", 1)[0].strip('"')
        if first_field and first_field != "INFO:":
            holder["name"] = first_field
    return holder
