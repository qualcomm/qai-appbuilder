# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background process manager - readiness probe primitives.

This module owns the **real-state-first** readiness probe for tracked
background processes (AGENTS.md "State-Truth-First" Iron Rule 1):

- :func:`connected` does an actual TCP ``open_connection`` against the
  target ``host:port``.  No in-process flags, no "we spawned it so it
  must be ready" optimism - we trust only what the kernel says.
- :func:`compile_pattern` validates the user-supplied regex
  **synchronously, before** the child process is spawned, so a bad
  pattern surfaces as :class:`InvalidReadyPattern` to the caller
  instead of getting lost in the spawn pipeline.
- :func:`wait_for_ready` is the FIRST_COMPLETED race that the manager
  awaits after spawn: it returns ``True`` as soon as **either** the
  caller signals the ``ready_event`` (e.g. because the pattern matched
  on a freshly-appended stdout chunk) **or** the port becomes
  connectable - whichever wins first.  ``False`` means the readiness
  window elapsed (``timeout_s``) or the process is already terminal.

Design contract: see
``docs/90-refactor/background-process-design.md`` section 5.7.

This module is stdlib-only (``asyncio`` + ``re``) so it stays cheap to
import from anywhere in the package and carries no FastAPI / DI / SQL
import chain.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from qai.platform.background_process.ports import InvalidReadyPattern

__all__ = [
    "LOOPBACK_HOST",
    "PORT_PROBE_INTERVAL_S",
    "PORT_PROBE_TIMEOUT_S",
    "READY_DEFAULT_TIMEOUT_S",
    "compile_pattern",
    "connected",
    "wait_for_ready",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOOPBACK_HOST: str = "127.0.0.1"
"""IPv4 loopback — the only host this module ever probes.

Readiness probing is, by design, **about our own spawned child
process** on this machine; the manager never probes a remote host
(``docs/90-refactor/background-process-design.md`` §5.7). Loopback is
therefore the semantically-correct, non-configurable target — not a
user-tunable setting. Centralised as a constant so callers can read
``LOOPBACK_HOST`` instead of repeating the literal, and so the
``check_no_magic_host_port.py`` CI guard has a single, self-documenting
reference to allowlist (the module is stdlib-only and cannot import
the full Settings stack).
"""

READY_DEFAULT_TIMEOUT_S: float = 30.0
"""Default readiness window when ``Ready.timeout`` is not set.

Defaults to 30 seconds.  Callers (the manager) divide
``Ready.timeout`` (milliseconds) by 1000 themselves; this constant is
only used when the caller did not supply a timeout at all.
"""

PORT_PROBE_TIMEOUT_S: float = 0.5
"""Per-attempt TCP connect timeout for :func:`connected`.

Short enough that an
unreachable host returns quickly, long enough that a busy loopback
listener still accepts.
"""

PORT_PROBE_INTERVAL_S: float = 0.25
"""Sleep between :func:`connected` attempts inside the port-poll loop.

Total probe rate is ~4 Hz against the
target port - cheap on loopback, invisible on remote hosts.
"""


# ---------------------------------------------------------------------------
# connected()
# ---------------------------------------------------------------------------


async def connected(
    port: int,
    host: str = LOOPBACK_HOST,
    timeout: float = PORT_PROBE_TIMEOUT_S,
) -> bool:
    """Return ``True`` iff a TCP connection to ``host:port`` succeeds.

    Implementation opens a real
    socket, close it cleanly on success, treat any error or timeout as
    "not ready yet".  The close path itself swallows :class:`OSError`
    because some servers reset the connection immediately after accept
    (e.g. wrong protocol on the wire) - that still proves the port is
    accepting, which is all we care about here.

    Args:
        port: Target TCP port (1..65535).
        host: Target host.  Defaults to IPv4 loopback - this is the
            only thing the manager probes; remote-host readiness is
            out of scope.
        timeout: Per-attempt connect timeout in seconds.  Defaults to
            :data:`PORT_PROBE_TIMEOUT_S`.

    Returns:
        ``True`` if the kernel accepted our SYN; ``False`` for any
        ``OSError`` / ``ConnectionRefusedError`` /
        :class:`asyncio.TimeoutError` / unexpected exception.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    except Exception:  # pragma: no cover - defensive; asyncio rarely raises non-OSError
        return False

    # Connection accepted - close cleanly.  Some servers RST on close
    # (or the peer drops mid-handshake); the success signal was the
    # accept itself, so swallow close errors.
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        del reader  # silence "unused" without changing observable behaviour
    return True


# ---------------------------------------------------------------------------
# compile_pattern()
# ---------------------------------------------------------------------------


def compile_pattern(pattern_str: str | None) -> re.Pattern[str] | None:
    """Compile a user-supplied readiness regex, raising on bad input.

    Called by the manager **before** spawning the child so a malformed
    pattern surfaces as :class:`InvalidReadyPattern` to the caller.
    An empty / ``None`` pattern is a no-op
    (returns ``None``); the manager then skips the pattern-match leg of
    the readiness race entirely.

    Args:
        pattern_str: A ``re.compile``-compatible Python regex, or
            ``None`` / ``""`` to disable pattern matching.

    Returns:
        The compiled :class:`re.Pattern` for non-empty input, or
        ``None`` if matching is disabled.

    Raises:
        InvalidReadyPattern: ``pattern_str`` failed
            :func:`re.compile` (re-raised synchronously, with the
            original message embedded for caller diagnostics).
    """
    if not pattern_str:
        return None
    try:
        return re.compile(pattern_str)
    except re.error as exc:
        raise InvalidReadyPattern(
            f"Invalid ready pattern: {pattern_str!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# wait_for_ready()
# ---------------------------------------------------------------------------


async def wait_for_ready(
    *,
    port: int | None,
    timeout_s: float,
    is_terminal: Callable[[], bool],
    ready_event: asyncio.Event,
) -> bool:
    """Race ``ready_event`` against the port-poll loop and a timeout.

    The caller (manager) is responsible for the **pattern** half of
    the readiness probe: when a stdout/stderr chunk arrives and the
    compiled pattern matches the accumulated output, the caller calls
    ``ready_event.set()``.  This function takes care of the orthogonal
    **port** half (real TCP probe via :func:`connected`) and the
    overall **timeout**.  Whichever fires first wins:

    - ``ready_event`` set (by caller's pattern match, or by us when
      ``port`` becomes connectable) -> return ``True``.
    - ``timeout_s`` elapses -> return ``False``.
    - ``is_terminal()`` becomes ``True`` (process died mid-probe) ->
      return ``False`` immediately, regardless of port state.

    Args:
        port: TCP port to poll, or ``None`` to disable port polling
            (caller is then the only path to ``ready_event``).
        timeout_s: Overall readiness window in seconds.  Use
            :data:`READY_DEFAULT_TIMEOUT_S` when the caller has no
            explicit ``Ready.timeout``.
        is_terminal: Cheap callback returning ``True`` once the child
            has entered any terminal status (``exited`` / ``failed`` /
            ``stopped``).  Checked at every poll tick so we do not
            keep probing a dead port.
        ready_event: Caller-owned :class:`asyncio.Event`.  Set by the
            caller on pattern match; also set by us on successful
            port probe so the caller's ``ready_event.wait()`` callers
            see a single unified signal.

    Returns:
        ``True`` if ``ready_event`` fired before the timeout (and the
        process did not enter a terminal status first); ``False``
        otherwise.

    Notes:
        - Returns immediately with ``True`` if ``ready_event`` is
          already set on entry (caller pre-matched the pattern on
          initial buffer content).
        - Returns immediately with ``False`` if ``is_terminal()`` is
          already ``True`` on entry.
        - All spawned tasks are cancelled and awaited before this
          coroutine returns, so the caller never leaks a pending
          poll task.
    """
    # Fast paths - check current state before spawning any tasks.
    if is_terminal():
        return False
    if ready_event.is_set():
        return True

    # The wait task is shared by all branches; port-poll is optional.
    wait_task = asyncio.create_task(
        ready_event.wait(), name="bgp.ready.wait"
    )
    poll_task: asyncio.Task[None] | None = None
    if port is not None:
        poll_task = asyncio.create_task(
            _port_poll_loop(
                port=port,
                is_terminal=is_terminal,
                ready_event=ready_event,
            ),
            name=f"bgp.ready.port_poll[{port}]",
        )

    pending: set[asyncio.Task[object]] = set()
    try:
        # ``asyncio.wait`` with a timeout covers the FIRST_COMPLETED
        # race.  When ``ready_event.set()`` fires (either via caller
        # pattern match or via _port_poll_loop), wait_task wakes; if
        # neither side fires, we wake up here on timeout.
        watched: set[asyncio.Task[object]] = {wait_task}
        if poll_task is not None:
            watched.add(poll_task)

        done, pending = await asyncio.wait(
            watched,
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Decide the result strictly from observable state, not from
        # "which task finished" - the truth is whether
        # ready_event.is_set() before the deadline AND the process is
        # not already terminal.
        if is_terminal():
            return False
        if ready_event.is_set():
            return True
        # Timed out (or only poll_task finished without setting the
        # event, which the poll loop never does on its own).
        del done  # silence unused-binding warnings on some linters
        return False
    finally:
        # Always cancel + await leftovers so the manager never leaks a
        # background poll task into the next readiness window.
        for task in pending:
            task.cancel()
        # Cover the case where wait_task / poll_task were never put in
        # ``pending`` because they completed (still need to drain them).
        for task in (wait_task, poll_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # Suppress - this is best-effort cleanup; the race
                # winner already determined our return value above.
                pass


async def _port_poll_loop(
    *,
    port: int,
    is_terminal: Callable[[], bool],
    ready_event: asyncio.Event,
) -> None:
    """Poll ``port`` until it accepts, the event is set, or terminal.

    Internal helper for :func:`wait_for_ready`.  Try
    :func:`connected`, sleep, repeat.  On first successful connect,
    set ``ready_event`` so the parent ``wait_for_ready`` sees the
    same signal as a caller-driven pattern match.

    Exits silently when:

    - ``ready_event`` is already set (someone else won the race).
    - ``is_terminal()`` returns ``True`` (process died).
    - :class:`asyncio.CancelledError` is raised by the parent's
      cleanup ``finally`` block.

    Never raises - any exception from :func:`connected` is already
    handled there (returns ``False``).
    """
    while not ready_event.is_set() and not is_terminal():
        if await connected(port):
            # Signal the parent; do NOT mutate any manager-side state
            # here - the manager observes ready_event and applies the
            # status transition (``starting`` -> ``ready``) itself.
            ready_event.set()
            return
        try:
            await asyncio.sleep(PORT_PROBE_INTERVAL_S)
        except asyncio.CancelledError:
            raise
