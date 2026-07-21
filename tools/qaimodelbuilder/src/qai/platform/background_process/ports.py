# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background process manager - Port + value objects.

This module is the typed boundary between consumers (LLM tool layer,
HTTP API, lifespan hooks) and the concrete
:class:`SubprocessBackgroundProcessManager` adapter. Everything declared
here is a frozen dataclass (no business logic, no IO) so the contract
can be imported from any context without dragging in FastAPI / DI /
SQLAlchemy.

Field renames go ``camelCase -> snake_case`` only - semantics, value
ranges, and enum members are preserved verbatim so the LLM tool layer
can re-export the tool description text without rewording (see
``tool_schemas.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from qai.platform.tool_docs import SHELL_ALIAS_ENUM

__all__ = [
    "BackgroundProcessManagerPort",
    "Info",
    "InvalidReadyPattern",
    "Lifetime",
    "Logs",
    "ManagerError",
    "ProcessNotFound",
    "Ready",
    "ReadyPortInUse",
    "StartInput",
    "Status",
    "TERMINAL_STATUSES",
    "Time",
]


# ---------------------------------------------------------------------------
# Enums (string literals to keep wire format simple)
# ---------------------------------------------------------------------------

Status = Literal[
    "starting",
    "running",
    "ready",
    "exited",
    "failed",
    "stopping",
    "stopped",
]
"""Lifecycle status of a tracked background process.

Transitions:

``starting`` -> (``ready`` | ``running``) -> (``stopping`` ->
``stopped`` | ``exited`` | ``failed``).

``ready`` is reached when the user's ``Ready`` probe (pattern / port)
fires. ``running`` is the steady state after the ``ready.timeout``
elapses without the probe firing - the process is alive but readiness
is unknown.
"""


Lifetime = Literal["session"]
"""Ownership envelope for a tracked process.

- ``session`` (default and only value): killed when the owning session
  ends (:meth:`BackgroundProcessManagerPort.stop_session`) or the
  daemon shuts down (:meth:`BackgroundProcessManagerPort.shutdown`).
"""


TERMINAL_STATUSES: frozenset[str] = frozenset({"exited", "failed", "stopped"})
"""Status values from which no further state transition is allowed."""


# ---------------------------------------------------------------------------
# Value objects (DTOs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Time:
    """Lifecycle timestamps (ms since Unix epoch, UTC)."""

    started: int
    updated: int
    ended: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.started, int) or self.started < 0:
            raise ValueError(
                f"started must be non-negative int, got {self.started!r}"
            )
        if not isinstance(self.updated, int) or self.updated < 0:
            raise ValueError(
                f"updated must be non-negative int, got {self.updated!r}"
            )
        if self.ended is not None and (
            not isinstance(self.ended, int) or self.ended < 0
        ):
            raise ValueError(
                f"ended must be non-negative int or None, got {self.ended!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Ready:
    """Optional readiness probe configuration for ``start``.

    At least one of
    ``pattern`` / ``port`` must be set for the probe to be meaningful;
    a ``Ready`` with neither field set is equivalent to omitting the
    probe (the manager marks the process as ``running`` immediately).

    ``pattern`` is a Python regex pattern (``re.compile``-compatible)
    matched against the accumulated stdout/stderr output, following
    JavaScript ``RegExp`` semantics, which is a near-superset of
    Python's ``re`` for the simple patterns LLMs typically generate
    (``"ready"`` / ``"Local:"`` / ``"started server"``).
    """

    pattern: str | None = None
    port: int | None = None
    timeout: int | None = None  # milliseconds

    def __post_init__(self) -> None:
        if self.pattern is not None and not isinstance(self.pattern, str):
            raise TypeError(
                f"pattern must be str or None, got {type(self.pattern).__name__}"
            )
        if self.port is not None:
            if not isinstance(self.port, int) or isinstance(self.port, bool):
                raise TypeError(
                    f"port must be int or None, got {type(self.port).__name__}"
                )
            if not (0 < self.port < 65536):
                raise ValueError(f"port must be in 1..65535, got {self.port}")
        if self.timeout is not None:
            if not isinstance(self.timeout, int) or isinstance(
                self.timeout, bool
            ):
                raise TypeError(
                    "timeout must be int or None, got "
                    f"{type(self.timeout).__name__}"
                )
            if self.timeout <= 0:
                raise ValueError(
                    f"timeout must be > 0 when set, got {self.timeout}"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class Info:
    """Externally-visible state of one tracked background process.

    Every field is
    present 1:1 (``camelCase -> snake_case`` only). Frozen so consumers
    can pass it around safely; the manager produces a fresh instance
    on every state transition.

    Frontend / sidebar contract reads ``status`` /
    ``description`` / ``command`` / ``pid`` / ``ports`` - these fields
    MUST keep their semantics.
    """

    id: str
    session_id: str
    pid: int | None
    command: str
    cwd: str
    description: str | None
    ports: tuple[int, ...]
    status: Status
    lifetime: Lifetime
    ready: bool
    exit_code: int | None
    signal: str | None
    output: str
    time: Time
    # 2026-07-13 (D2-C): FileGuard denial diagnostics filled by
    # ``SubprocessBackgroundProcessManager._on_exit`` when the subprocess
    # died non-zero AND the composition root injected a native denial
    # probe. Empty string (the default) when no probe was wired, when the
    # probe found no matching DENY audit rows, or when the process exited
    # cleanly.
    #
    # Rendered by the chat tool result renderer (D2-D) as a trailing block
    # on ``logs`` / ``status`` output so the LLM sees the authoritative
    # cause ("blocked by native FileGuard, not by filesystem ACL") and
    # does not retry via elevated / alternate tools / reformatted paths.
    #
    # The string is already ``"\n\n"``-prefixed when non-empty (produced
    # by :func:`qai.security.domain.native_guard_denial_message.
    # build_native_guard_denial_note`); consumers append it verbatim.
    exit_diagnostics: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.startswith("bgp"):
            raise ValueError(
                f"id must be a str starting with 'bgp', got {self.id!r}"
            )
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty str")
        if self.pid is not None and (
            not isinstance(self.pid, int) or self.pid <= 0
        ):
            raise ValueError(
                f"pid must be positive int or None, got {self.pid!r}"
            )
        if not isinstance(self.command, str) or not self.command:
            raise ValueError("command must be a non-empty str")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("cwd must be a non-empty str")
        if not isinstance(self.ports, tuple):
            raise TypeError(
                f"ports must be a tuple of int, got {type(self.ports).__name__}"
            )
        for i, port in enumerate(self.ports):
            if not isinstance(port, int) or port <= 0 or port >= 65536:
                raise ValueError(f"ports[{i}] invalid: {port!r}")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise TypeError(
                "exit_code must be int or None, got "
                f"{type(self.exit_code).__name__}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class StartInput:
    """Caller's intent for :meth:`BackgroundProcessManagerPort.start`.

    Only the
    ``session`` lifetime is supported (the manager kills every
    tracked process when its owning session ends or the daemon shuts
    down — no parent-transfer / persistent variants).

    ``extra_env`` is an optional overlay of environment variables the
    caller wants the child to receive **in addition to** the manager's
    baseline child env. It is applied by the manager *after* the
    credential-strip in :func:`env_strip.build_child_env`, so it can
    only *add* / *override* non-credential names. This is the supported
    way to inject non-secret runtime config (e.g. ``APP_ROOT`` /
    ``PYTHONPATH`` / QAIRT ``PATH`` segments) that the baseline env does
    not carry. Credential-shaped names still flow through
    ``SecretStore`` (AGENTS.md §3.3) and MUST NOT be smuggled here.
    """

    session_id: str
    command: str
    cwd: str | None = None
    description: str | None = None
    ready: Ready | None = None
    lifetime: Lifetime = "session"
    extra_env: Mapping[str, str] | None = None
    # 2026-07-13: caller-selected shell name. ``None`` (default) keeps the
    # existing behaviour of :func:`shell.acceptable()` auto-selection (pwsh /
    # powershell / cmd on Windows; bash / zsh on POSIX). Non-None values are
    # the LLM-facing aliases exposed by ``tool_schemas``:
    #
    #   ``"auto"``       — same as None (auto-select).
    #   ``"sh"``         — force PortableGit bash.exe (bash back-end;
    #                      required for POSIX tools like ``ls`` / ``grep`` /
    #                      ``mv`` / ``rm`` / ``git`` scripts).
    #   ``"powershell"`` — force PowerShell 5.1 (``powershell.exe``).
    #   ``"cmd"``        — force ``cmd.exe``.
    #
    # The manager resolves this to an absolute executable path via the same
    # PortableGit-aware lookup that the ``exec`` tool uses, so the LLM sees a
    # consistent shell-selection surface across both tools.
    shell: str | None = None
    # 2026-07-09: opt-in flag for native FileGuard guard-token injection.
    # DEFAULT False — the guard-token is only injected when the caller
    # explicitly requests it. This ensures that only LLM tool-call paths
    # (which need the native file-guard to protect against model-generated
    # dangerous commands) receive the token; internal callers such as
    # AppProjectProcessManager (App Builder Run button) do NOT inject it,
    # so their child processes are not subject to the native hook and will
    # never trigger spurious authorization dialogs for normal file access
    # (e.g. writing to Temp). Callers that do NOT set this flag get the
    # same allow-all behaviour as before the guard was introduced.
    inject_file_guard: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty str")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("command must be a non-empty (after-strip) str")
        if self.lifetime != "session":
            raise ValueError(
                f"lifetime must be 'session', got {self.lifetime!r}"
            )
        if self.extra_env is not None:
            if not isinstance(self.extra_env, Mapping):
                raise TypeError(
                    "extra_env must be a Mapping[str, str] or None, got "
                    f"{type(self.extra_env).__name__}"
                )
            for key, value in self.extra_env.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(
                        f"extra_env keys must be non-empty str, got {key!r}"
                    )
                if not isinstance(value, str):
                    raise TypeError(
                        f"extra_env[{key!r}] must be str, got "
                        f"{type(value).__name__}"
                    )
        # 2026-07-13: shell name must be one of the LLM-facing aliases (or
        # None / "auto" for auto-select). The enum is shared with the
        # ``exec`` tool via :data:`qai.platform.tool_docs.SHELL_ALIAS_ENUM`
        # so both tools present a consistent surface to the LLM. The
        # manager resolves each alias to a concrete PortableGit / system
        # shell path.
        if self.shell is not None:
            if not isinstance(self.shell, str):
                raise TypeError(
                    f"shell must be a str or None, got {type(self.shell).__name__}"
                )
            if self.shell not in SHELL_ALIAS_ENUM:
                raise ValueError(
                    f"shell must be one of {list(SHELL_ALIAS_ENUM)}, "
                    f"got {self.shell!r}"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class Logs:
    """Tail output snapshot returned by :meth:`BackgroundProcessManagerPort.logs`.

    Aligned with the ``Logs`` shape. ``output`` is
    the full retained tail (capped at 200 KiB UTF-8, with
    continuation-byte safe truncation - see
    :mod:`qai.platform.background_process.buffer`).
    """

    id: str
    session_id: str
    output: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ManagerError(Exception):
    """Base for all errors raised by the background-process manager."""


class InvalidReadyPattern(ManagerError):
    """``Ready.pattern`` failed to compile as a regex.

    Raised synchronously by :meth:`BackgroundProcessManagerPort.start`
    BEFORE the child process is spawned.
    """


class ReadyPortInUse(ManagerError):
    """``Ready.port`` was already accepting TCP connections at start.

    Raised synchronously BEFORE the child is spawned.
    Prevents the false-positive where some
    other server on the same port makes our process look "ready"
    immediately.
    """


class ProcessNotFound(ManagerError):
    """``process_id`` was not found in any state map.

    Raised by HTTP handlers; the LLM tool layer returns a friendly
    string instead. The manager's public API methods return ``None``
    for missing ids (a nullable return shape).
    """


# ---------------------------------------------------------------------------
# Port Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BackgroundProcessManagerPort(Protocol):
    """Manage long-running background subprocesses with structured lifecycle.

    Methods correspond 1:1 to the 8 core exports - see
    ``docs/90-refactor/background-process-design.md`` section 3.4 for
    the contract table. Implementations MUST:

    - Maintain per-process accumulated stdout/stderr capped at 200 KiB
      (continuation-byte-safe; see :mod:`buffer`).
    - Publish ``BackgroundProcessUpdated`` / ``BackgroundProcessDeleted``
      events on the shared :class:`qai.platform.events.EventBus`.
    - Strip credential environment variables from the child env
      (see :mod:`env_strip`).
    - Apply :class:`qai.platform.process.ProcessKillGroup` (Win32 Job
      Object KILL_ON_JOB_CLOSE) to every spawned child as the
      AGENTS.md section "Iron Rule 5" parent-crash safeguard.
    """

    async def start(self, input: StartInput) -> Info:
        """Spawn a new tracked background process.

        Raises:
            InvalidReadyPattern: if ``input.ready.pattern`` is not a
                valid regex (no process is spawned).
            ReadyPortInUse: if ``input.ready.port`` is already accepting
                TCP connections (no process is spawned).
            ManagerError: for other lifecycle errors during spawn.
            OSError: passed through when
                ``asyncio.create_subprocess_exec`` raises (e.g. ENOENT
                for the shell binary).
        """
        ...

    async def list(self, *, session_id: str | None = None) -> Sequence[Info]:
        """Snapshot the tracked processes.

        If ``session_id`` is provided, returns only the processes
        belonging to that session.
        """
        ...

    async def get(self, process_id: str) -> Info | None:
        """Fetch a single process by id. Returns ``None`` if not found."""
        ...

    async def logs(self, process_id: str) -> Logs | None:
        """Return the retained output tail. ``None`` if id not found.

        ``Logs.output`` is the full accumulated buffer (<= 200 KiB,
        UTF-8). No cursor / incremental modes are supported - callers
        that need incremental updates should subscribe to the
        ``BackgroundProcessUpdated`` event stream.
        """
        ...

    async def stop(self, process_id: str) -> Info | None:
        """Terminate a process and its child process tree.

        Returns the post-stop ``Info`` (status will be ``stopping`` /
        ``stopped`` / ``exited`` / ``failed`` depending on race
        outcomes), or ``None`` if the id was not found. Best-effort:
        if the kill cascade fails the record is still updated to a
        terminal state and the failure is logged.
        """
        ...

    async def restart(self, process_id: str) -> Info | None:
        """Stop, then re-spawn with the same command + lifetime.

        Reuses the original ``process_id``.
        Returns the new ``Info`` (same ``id``, new ``pid`` + reset
        timestamps), or ``None`` if id not found.
        """
        ...

    async def stop_session(self, session_id: str) -> None:
        """Terminate all session processes belonging to this session.

        Every tracked process whose ``session_id`` matches is killed
        (process tree) and removed from the manager. Idempotent: an
        unknown ``session_id`` is a no-op.
        """
        ...

    async def shutdown(self) -> None:
        """Clean up everything when the daemon is shutting down.

        All tracked processes are terminated and removed.

        Idempotent: calling shutdown twice is a no-op on the second
        call.
        """
        ...


# Silence "imported but unused" for ``field`` - it is exported via this
# module's dataclass machinery (downstream extensions of frozen
# dataclasses can use ``field(default_factory=...)``).
_ = field
