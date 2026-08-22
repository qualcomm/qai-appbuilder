# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background-process manager - subprocess-backed implementation.

This module is the concrete adapter that implements
:class:`qai.platform.background_process.ports.BackgroundProcessManagerPort`
on top of stdlib ``asyncio.subprocess`` plus the Wave A / Wave B helpers
in this same sub-package. It is the runtime brain that backs every
``background_process`` LLM tool call, every ``/api/background_process``
HTTP route, and the lifespan-driven session/shutdown drain.

Design references:

* ``docs/90-refactor/background-process-design.md`` sections 3 (data
  contract), 5 (service core algorithm), 11 (State-Truth-First iron-rule
  self-check).

Lifecycle support
-----------------

This adapter implements a single in-memory lifetime: ``session``.
Tracked processes are killed when their owning session ends
(:meth:`stop_session`) or the daemon shuts down (:meth:`shutdown`).
There is no cross-session transfer or persistent variant — when the
application exits, every spawned child exits too.

The child is spawned via
:func:`qai.platform.background_process.shell.build_argv`, assigned to
the manager's :class:`ProcessKillGroup` (Win32 ``KILL_ON_JOB_CLOSE`` Job
Object) for hard-parent-death cleanup, and tracked in
:attr:`State.processes` until it exits or is terminated.

Terminal-record retention (P7, 2026-08-09)
------------------------------------------

Settling does not immediately un-track a record: it stays queryable for
:data:`_RETENTION_S` (5 minutes), then is evicted and announced with
:class:`BackgroundProcessDeleted`. This exists so the most common
"where did my process go" case — it just finished and the client
re-asked, e.g. a page reload — gets the AUTHORITATIVE terminal state
instead of a 404 the client has to paper over with a cached snapshot.
After the window a lookup is a plain miss; no tombstone is kept.

What is retained is the RECORD, not the process: the child is already
reaped, so ``stop`` on a retained record is a truthful no-op that keeps
the real ``exited`` / ``failed`` status rather than claiming a
``stopped`` one, and ``restart`` re-spawns the command under the same
id (see :meth:`stop` / :meth:`restart`).

Iron-rule self-check (AGENTS.md "State-Truth-First")
----------------------------------------------------

1. **Real-state probe first** -- :meth:`get` / :meth:`list` return the
   in-memory ``Active.info`` which itself reflects ``Popen.returncode``
   (transitions to ``exited`` / ``failed`` happen in the ``_on_exit``
   wait task, never on a soft flag).
2. **Defense-in-depth orphan kill** -- every spawned child is
   assigned to the manager's :class:`ProcessKillGroup`
   (``KILL_ON_JOB_CLOSE`` Win32 Job Object), so a hard parent death
   still cleans up the children at OS level.
3. **No bus-event truth bypass** -- ``BackgroundProcessUpdated`` is a
   value snapshot of ``info``; subscribers never get a writable
   handle and cannot fork the truth source.
4. **No optimistic state cache** -- the ``Active.output_bytes`` ring
   buffer is rebuilt from real stdout/stderr stream bytes; ``info.output``
   is materialised on every publish from the byte truth source.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.background_process import buffer as buffer_module
from qai.platform.background_process import env_strip as env_strip_module
from qai.platform.background_process import kill as kill_module
from qai.platform.background_process import ready as ready_module
from qai.platform.background_process import shell as shell_module
from qai.platform.background_process.events import (
    BackgroundProcessDeleted,
    BackgroundProcessUpdated,
)
from qai.platform.background_process.id import new_bgp_id
from qai.platform.background_process.ports import (
    BackgroundProcessManagerPort,
    Info,
    Logs,
    ManagerError,
    Ready,
    ReadyPortInUse,
    StartInput,
    Status,
    TERMINAL_STATUSES,
    Time,
)
from qai.platform.events import EventBus
from qai.platform.process import no_window_creationflags

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from qai.platform.process import ProcessKillGroup

logger = logging.getLogger("qai.platform.background_process.manager")


# ---------------------------------------------------------------------------
# Constants (design doc section 5.2)
# ---------------------------------------------------------------------------

OUTPUT_BYTE_CAP: int = buffer_module.DEFAULT_CAP_BYTES
"""Ring-buffer cap (200 KiB) - re-exported for callers that import from us."""

KILL_GRACE_S: float = kill_module.KILL_GRACE_S
"""SIGTERM -> SIGKILL escalation window (3.0 s)."""

READY_DEFAULT_S: float = ready_module.READY_DEFAULT_TIMEOUT_S
"""Default readiness window when ``Ready.timeout`` is not set (30.0 s)."""

PUBLISH_DEBOUNCE_S: float = 0.5
"""Per-process bus-publish coalescing window."""

DESCRIPTION_FALLBACK_MAX_CHARS: int = 120
"""Ceiling for a command-line-derived fallback ``description`` (P2 §2).

``adopt()`` / ``start()`` fall back to the command line itself when the
caller sends no ``description`` — concurrent job attribution
(``── Job <id> (<label>) ──``, see P0 §3.3) depends on the label being
non-empty, and the exec auto-background path never gives a model the
chance to supply one. Over this many characters the fallback is cut to
:data:`DESCRIPTION_FALLBACK_TRUNCATED_CHARS` chars plus an ``"..."``
suffix (see :func:`_derive_description`).
"""

DESCRIPTION_FALLBACK_TRUNCATED_CHARS: int = 117
"""Kept-character count when a fallback description is truncated.

``117 + len("...") == 120 == DESCRIPTION_FALLBACK_MAX_CHARS`` — the
truncated form is exactly as long as the ceiling it exists to enforce.
"""

_RETENTION_S: float = 300.0
"""How long a TERMINAL record stays queryable after removal from the live
set (P7). Chosen to give "the process just finished, the user reloads the
page" (P4's root cause) a real window to observe the authoritative terminal
state instead of only a client-held snapshot. Purely a query-retention
knob — it does not resurrect the process (`stop`/`restart` on a retained
record are ``ProcessNotFound``-equivalent no-ops; see ``_terminate``)."""


# ---------------------------------------------------------------------------
# D2-C: native FileGuard denial probe type alias.
#
# ``qai.platform.background_process`` is bound by the same import-linter
# ``context-isolation`` contract as ``qai.ai_coding`` — it MUST NOT import
# ``qai.security.**`` directly (only ``qai.** -> qai.platform.**`` crosses
# contexts). To surface FileGuard denials on subprocess exit without
# violating the contract, the composition root (``apps/api/``) pre-composes
# :func:`qai.security.application.ports.AuditQueryPort.
# query_native_denies_by_pid_tree` + :func:`qai.security.domain.
# native_guard_denial_message.build_native_guard_denial_note` into a single
# stdlib-typed async callable and injects it into
# :class:`SubprocessBackgroundProcessManager` via ``native_denial_probe``.
#
# The callable takes ``(root_pid, since_utc)`` and MUST return a
# ready-to-append note string:
#
# * ``""`` — no matching DENY rows, audit disabled, or audit failed. The
#   manager leaves ``Info.exit_diagnostics`` empty and the LLM sees only
#   the raw stdout/stderr (pre-D2-C behaviour).
# * ``"\\n\\n<block>"`` — one or more denies attributable to the exited
#   subprocess tree. The block is already ``"\\n\\n"``-prefixed by
#   :func:`build_native_guard_denial_note`; consumers append verbatim.
#
# Contract: MUST NOT raise. The manager still wraps every call in
# ``try/except Exception`` as a fail-open defense-in-depth belt.
NativeGuardDenialProbe = Callable[[int, datetime], Awaitable[str]]


# ---------------------------------------------------------------------------
# Internal state dataclasses (design doc section 5.1)
# ---------------------------------------------------------------------------


@dataclass
class State:
    """In-memory task map for tracked ``session``-lifetime processes."""

    processes: dict[str, Active] = field(default_factory=dict)


@dataclass
class Active:
    """Mutable per-process runtime record.

    ``info`` is the externally-visible immutable snapshot; we replace
    it (via :func:`dataclasses.replace`) on every transition so
    subscribers that hold a reference see a stable picture.
    ``output_bytes`` is the bytes truth source; ``info.output`` is the
    UTF-8 decode of it produced on each publish boundary.
    """

    info: Info
    proc: asyncio.subprocess.Process | None
    start_input: StartInput
    pattern: re.Pattern[str] | None
    output_bytes: bytes
    publish_handle: asyncio.TimerHandle | None
    ready_event: asyncio.Event
    disposed: bool = False
    # 2026-07-13 (D2-C): wall-clock stamp captured immediately BEFORE
    # ``asyncio.create_subprocess_exec`` so ``_on_exit`` can pass a
    # ``since`` filter to :attr:`native_denial_probe`. Must be an aware
    # UTC datetime (matches the tz that ``AuditEntry.occurred_at`` is
    # stored with — a naive value would be interpreted as local time by
    # the SQL adapter and miss recent DENY rows). ``None`` on manager
    # instances that predate the field-init path (defensive default);
    # the exit-diagnostics query short-circuits when the stamp is
    # missing.
    spawn_started_at: datetime | None = None
    # 2026-08-09 (P7): pending retention timer for a TERMINAL record. Set by
    # ``_schedule_eviction`` when the record settles and cleared when it
    # fires or is cancelled (``restart`` reuses the id; ``shutdown`` drops
    # everything). Non-``None`` means "already terminal, queryable, awaiting
    # eviction" — the record is retained state, NOT a running process.
    evict_handle: asyncio.TimerHandle | None = None
    #: True when this record came from :meth:`adopt` rather than
    #: :meth:`start` — i.e. the process was spawned elsewhere (the exec
    #: auto-background path) and handed over already running.  Its
    #: ``start_input`` is therefore SYNTHETIC: no shell selection, no exec
    #: env, no FileGuard injection.  :meth:`restart` refuses such a record
    #: rather than re-launching the bare command under different conditions
    #: than the original run.
    adopted: bool = False


# ---------------------------------------------------------------------------
# Shell resolution
# ---------------------------------------------------------------------------


def _resolve_portable_git_shell(shell_name: str) -> str | None:
    r"""Resolve PortableGit's ``sh.exe`` / ``bash.exe`` to an absolute path.

    Mirrors the resolver used by the ``exec`` tool
    (``qai.ai_coding.infrastructure.tools.handlers.exec._resolve_portable_git_shell``)
    so ``background_process`` and ``exec`` present a consistent shell
    surface to the LLM. Kept local (not imported from ai_coding) to avoid a
    cross-module import (``background_process`` is a
    ``qai.platform`` bounded context; it must not depend on
    ``qai.ai_coding``).

    State-Truth-First: Windows has no ``sh`` on PATH; the anchor is
    ``%LOCALAPPDATA%\QAIModelBuilder\git``, matching Setup.bat / the
    exec-tool env builder. Returns None when PortableGit is absent or
    ``LOCALAPPDATA`` is empty; callers fall back to the shell auto-picker.

    Architecture: PortableGit ships both ``bin\`` and ``usr\bin\``; on
    WoS these can differ in PE machine (ARM64 vs x86-64). We return the
    first existing candidate — we do NOT try to arch-match the current
    process here because the caller path is a background daemon that may
    be a different arch from the eventual child; the child surfacing a
    normal spawn error is preferable to us pre-emptively disabling a
    working shell.
    """
    if sys.platform != "win32":
        return None
    if shell_name not in ("sh", "bash"):
        return None
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    git_root = Path(local_app_data) / "QAIModelBuilder" / "git"
    exe = f"{shell_name}.exe"
    candidates = [git_root / "bin" / exe, git_root / "usr" / "bin" / exe]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _resolve_shell_alias(alias: str | None) -> str:
    """Map the LLM-facing shell alias to a concrete shell path.

    Aliases (mirrors the ``exec`` tool for a consistent LLM UX):

    * ``None`` / ``"auto"`` — :func:`shell.acceptable` auto-select.
    * ``"sh"`` / ``"bash"``  — PortableGit ``bash.exe`` (POSIX tools such
                              as ``ls`` / ``grep`` / ``mv`` require it).
    * ``"powershell"``      — ``powershell.exe`` (PowerShell 5.1) on
                              PATH, else ``pwsh.exe``, else fallback.
    * ``"cmd"``             — ``cmd.exe`` (``%COMSPEC%`` fallback).

    Falls back to :func:`shell.acceptable` when a specific alias cannot
    be resolved on the current platform (e.g. ``"sh"`` on a machine
    without PortableGit). The caller (``start``) uses the returned path
    directly; :func:`shell.build_argv` dispatches on its basename.
    """
    if alias in (None, "", "auto"):
        return shell_module.acceptable()
    if alias in ("sh", "bash"):
        # LLM-facing ``sh``/``bash`` map to PortableGit's ``bash.exe`` (the
        # kind ``build_argv`` dispatches on is bash — MSYS bash is the
        # canonical POSIX shell on this platform). If PortableGit is
        # missing, fall back to the platform default so we still spawn.
        resolved = _resolve_portable_git_shell("bash")
        if resolved is not None:
            return resolved
        return shell_module.acceptable()
    if alias == "powershell":
        if sys.platform == "win32":
            for candidate in ("powershell.exe", "pwsh.exe"):
                found = shutil.which(candidate)
                if found:
                    return found
        return shell_module.acceptable()
    if alias == "cmd":
        if sys.platform == "win32":
            found = shutil.which("cmd.exe")
            if found:
                return found
            return os.environ.get("COMSPEC", "cmd.exe")
        return shell_module.acceptable()
    # Unreachable (StartInput.__post_init__ validates the alias set) but
    # tolerate defensively to keep the manager robust against upstream
    # schema drift.
    return shell_module.acceptable()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SubprocessBackgroundProcessManager(BackgroundProcessManagerPort):
    """Asyncio + subprocess implementation of the BG-process port.

    Lifetime contract: one instance per daemon (one EventBus, one
    ``data_root``). Compose via DI; do NOT instantiate per-request.

    Thread-safety: not thread-safe. All public methods must be awaited
    on the same asyncio event loop (the lifespan loop). Internal state
    mutations happen inside that loop; cross-loop access (e.g. from a
    nested TestClient) requires going through ``run_coroutine_threadsafe``.

    Constructor parameters
    ----------------------

    events:
        Shared :class:`EventBus` instance. Used by :meth:`_publish_now`
        to emit :class:`BackgroundProcessUpdated` /
        :class:`BackgroundProcessDeleted` to any subscriber (HTTP SSE,
        TUI sidebar, audit pipeline).

    data_root:
        Filesystem root for resolving relative ``cwd`` arguments to
        :meth:`start`. Resolved at construction time (``Path.resolve()``)
        so later chdir on the parent does not move the directory under
        our feet (AGENTS.md State-Truth-First iron-rule 4: paths use
        structural markers, never relative-fragile assumptions).

    kill_group:
        Optional :class:`ProcessKillGroup` (Win32 Job Object with
        ``KILL_ON_JOB_CLOSE``). When provided, every spawned child is
        assigned to it so a hard parent death cleans up children at OS
        level (iron-rule 5). ``None`` means no extra rail; the graceful
        kill paths still work.

    clock:
        Time source for ``Time.{started,updated,ended}`` stamps and
        for scheduling. Injected so unit tests can use a frozen clock.
        Default: ``time.time``.

    guard_token_provider:
        Optional zero-arg callable returning the live FileGuard
        guard-token (``str``) or ``None``. Injected by the ``apps/api``
        composition root (which is the only layer allowed to read the
        ``qai.security`` native-guard adapter). When it returns a
        non-empty token, every spawned child gets
        ``QAI_FILEGUARD_GUARD_TOKEN`` in its env so the native
        ``guard64.dll`` guards the child + its subtree (ASK pipeline);
        when it returns ``None`` (guard disabled / not started) nothing
        is injected and the child is bypassed (allow-all). Re-read per
        spawn (State-Truth-First) because the guard starts lazily in
        lifespan. ``None`` (the default) means no guard marker is ever
        injected — the pre-guard-reversal behaviour, and what tests use.

    allow_x86:
        When True, injects ``QAI_GUARD_ALLOW_X86=1`` into the child env
        so the native guard64 ``HookedCreateProcessW`` does not terminate
        32-bit (x86) children. Default False: 32-bit children are refused
        pre-spawn because guard64.dll cannot inject into them (security
        bypass). Injected by the composition root from
        ``settings.security.allow_x86_processes``.
    """

    def __init__(
        self,
        *,
        events: EventBus,
        data_root: Path,
        kill_group: "ProcessKillGroup | None" = None,
        clock: Callable[[], float] = time.time,
        guard_token_provider: Callable[[], str | None] | None = None,
        ask_pending_probe: "Callable[[int], bool] | None" = None,
        native_denial_probe: NativeGuardDenialProbe | None = None,
        allow_x86: bool = False,
    ) -> None:
        self._events = events
        self._data_root = Path(data_root).resolve()
        self._kill_group = kill_group
        self._clock = clock
        self._guard_token_provider = guard_token_provider
        self._allow_x86 = allow_x86
        # 2026-07-09 — probe(child_pid) → is a native FileGuard ASK pending on
        # it? Used by _wait_for_ready to PAUSE the readiness window while the
        # child is suspended awaiting a native file-access approval (parity with
        # exec's ask_pending_probe timeout postponement). ``None`` → readiness
        # timeout behaves as before (never pauses). Injected by apps/api.
        self._ask_pending_probe = ask_pending_probe
        # 2026-07-13 (D2-C) — probe(root_pid, since_utc) → ready-to-append
        # FileGuard denial diagnostic string. Injected by apps/api's
        # composition root (only layer allowed to touch qai.security);
        # ``None`` → ``Info.exit_diagnostics`` stays empty (pre-D2-C
        # behaviour, fail-open). See :data:`NativeGuardDenialProbe`.
        self._native_denial_probe = native_denial_probe

        self._state = State()
        self._shutdown_called = False

    # =========================================================
    # Public API (BackgroundProcessManagerPort)
    # =========================================================

    async def start(self, input: StartInput) -> Info:
        """Spawn a tracked background process. See port docstring."""
        if self._shutdown_called:
            raise RuntimeError(
                "BackgroundProcessManager.start() after shutdown()"
            )
        return await self._launch(input)

    async def list(
        self, *, session_id: str | None = None
    ) -> Sequence[Info]:
        """Snapshot current task records (session filter)."""
        out: list[Info] = []
        for active in list(self._state.processes.values()):
            if session_id is None or active.info.session_id == session_id:
                out.append(_clone_info(active.info))
        return out

    async def get(self, process_id: str) -> Info | None:
        """Fetch a single process by id. ``None`` if not found."""
        active = self._find(process_id)
        if active is None:
            return None
        return _clone_info(active.info)

    async def logs(self, process_id: str) -> Logs | None:
        """Return the retained tail. ``None`` if id not found."""
        active = self._find(process_id)
        if active is None:
            return None
        return Logs(
            id=active.info.id,
            session_id=active.info.session_id,
            output=buffer_module.decode_lossy(active.output_bytes),
        )

    #: Ceiling for a single ``wait`` call (seconds).  A wait must never be
    #: able to wedge a whole agent turn: the caller can always issue another
    #: ``wait`` if the process is still running when this budget elapses.
    WAIT_MAX_TIMEOUT_S: float = 120.0
    #: Poll cadence for ``wait``.  The exit path is event-driven inside the
    #: manager (``_on_exit`` flips the status), so this only bounds how fast
    #: the waiter notices; 250 ms keeps it responsive at negligible cost.
    _WAIT_POLL_S: float = 0.25

    async def wait(
        self, process_id: str, *, timeout_s: float,
    ) -> Info | None:
        """Block until the process settles or ``timeout_s`` elapses.

        See :meth:`BackgroundProcessManagerPort.wait`.  Returns the ``Info``
        as observed when the wait ends — terminal status means it finished,
        non-terminal means the budget ran out first.  ``None`` for an
        unknown id.  Never raises on timeout.
        """
        active = self._find(process_id)
        if active is None:
            return None
        # Already settled → return immediately (no sleep).
        if active.info.status in TERMINAL_STATUSES:
            return _clone_info(active.info)

        budget = max(0.0, min(float(timeout_s), self.WAIT_MAX_TIMEOUT_S))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while True:
            # Re-``_find`` each pass: a concurrent ``stop`` may have removed
            # the record, in which case the process is gone and we surface
            # the last snapshot we hold rather than looping forever.
            current = self._find(process_id)
            if current is None:
                return _clone_info(active.info)
            if current.info.status in TERMINAL_STATUSES:
                return _clone_info(current.info)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _clone_info(current.info)
            try:
                await asyncio.sleep(min(self._WAIT_POLL_S, remaining))
            except asyncio.CancelledError:
                # Turn torn down (user Stop / disconnect) — do not swallow.
                raise

    async def stop(self, process_id: str) -> Info | None:
        """Terminate a process; return post-stop info or ``None`` if unknown.

        The record stays queryable for :data:`_RETENTION_S` afterwards (P7)
        so the caller can read back the outcome it just caused.

        Already-terminal record (a process that exited on its own, still
        inside its retention window): no kill is attempted and the returned
        ``Info`` keeps its REAL status — ``exited`` / ``failed`` with the
        original exit code, never a synthesised ``stopped``. Retention is not
        re-armed either, so observing a dead process cannot extend how long
        its record survives. The call is a truthful no-op, not a claim to
        have stopped something that had already finished.
        """
        active = self._find(process_id)
        if active is None:
            return None
        await self._terminate(
            active, remove=True, silent=False, retain=True
        )
        return _clone_info(active.info)

    async def restart(self, process_id: str) -> Info | None:
        """Stop, then re-spawn with the same StartInput. Same id is reused.

        Works on a retained terminal record too, and means the same thing
        there as anywhere: re-run this command. The kill is skipped (already
        settled), the retention timer is cancelled with the old record, and
        the id is handed to a fresh spawn — so the outcome is a genuinely
        running process, not a "restarted" label over a corpse.

        REFUSES an adopted record (see :attr:`Active.adopted`). Those were
        spawned by ``exec`` with a selected shell, the exec environment and
        FileGuard injection; the synthetic ``StartInput`` kept for them
        carries none of that, so re-launching would run the same command line
        under materially different conditions and report success. Retention
        made this reachable — an adopted, exited record is now the most common
        thing sitting in the map — so failing loudly beats a silent mismatch.
        """
        active = self._find(process_id)
        if active is None:
            return None
        if active.adopted:
            raise ManagerError(
                f"restart: {process_id} was adopted from an exec run, not "
                f"started here — its shell / environment context is not "
                f"recorded, so it cannot be re-launched faithfully. Issue "
                f"the command again via exec instead."
            )
        original_input = active.start_input
        original_id = active.info.id
        # Retire the old record outright: no ``Deleted`` (the id lives on) and
        # NO retention — a retained record would be evicted out from under the
        # fresh spawn that is about to claim the same id.
        await self._terminate(active, remove=True, silent=True)
        # Re-spawn with the same id.
        new_info = await self._launch(original_input, id=original_id)
        return new_info

    async def stop_session(self, session_id: str) -> None:
        """Terminate every process owned by ``session_id``."""
        targets = [
            a
            for a in list(self._state.processes.values())
            if a.info.session_id == session_id
        ]
        for active in targets:
            try:
                await self._terminate(active, remove=True, silent=False)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "stop_session terminate failed id=%s", active.info.id
                )

    async def adopt(
        self,
        *,
        proc: asyncio.subprocess.Process,
        session_id: str,
        command: str,
        cwd: str,
        description: str | None = None,
    ) -> Info:
        """M5 auto-background: adopt an already-running subprocess.

        See :meth:`BackgroundProcessManagerPort.adopt` for the full contract.

        Implementation notes:

        * ``proc.stdout`` / ``proc.stderr`` are TAKEN OVER by the manager's
          own pump tasks (``_pump_stream``).  The caller (exec.py) must
          release its readers BEFORE calling adopt so the child does not
          block on a full pipe after exec stops draining.  See the exec
          soft-steer watcher for the release protocol (cancel the wait
          task, then call adopt — do NOT ``feed_eof`` the streams).
        * The child is added to the manager's Job Object so daemon shutdown
          still tears it down (defense-in-depth: exec spawned into the same
          process group in the first place; this is a no-op on re-add).
        * No ``StartInput`` was provided (adopt bypasses ``start``);
          a minimal synthetic ``StartInput`` is fabricated for the
          :class:`Active` record so downstream code (:meth:`restart` etc.)
          that assumes ``start_input`` exists still works — although
          ``restart`` on an adopted record cannot re-spawn identically (no
          shell / env context); attempting it will re-launch with a bare
          ``command`` string, which is intentional (adopted commands are
          expected to be one-shot).
        """
        if self._shutdown_called:
            raise ManagerError(
                "adopt: manager has been shut down; cannot adopt new proc"
            )
        if proc.returncode is not None:
            raise ManagerError(
                f"adopt: proc pid={proc.pid} already exited "
                f"(returncode={proc.returncode})"
            )
        # Defense-in-depth: also add to the manager's Job Object so daemon
        # shutdown tears it down even if exec's own kill-group release
        # semantics differ.
        if proc.pid is not None:
            try:
                kill_module.assign_to_job(self._kill_group, proc.pid)
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "bgp.adopt.assign_to_job_failed pid=%s", proc.pid
                )

        process_id = new_bgp_id()
        now_ms = self._now_ms()
        # Truth-first label fallback (P2 §2): the exec auto-background path
        # calls us with ``description=None`` — no model was ever asked — and a
        # nameless record is both a blank sidebar row and an unattributable
        # segment in a concurrent-completion delivery. Derive once, here, so
        # both the synthetic StartInput (which ``restart`` re-launches from)
        # and the Info carry the same label.
        resolved_description = _derive_description(description, command)
        # Synthetic StartInput — exec did not go through ``start`` so we
        # fabricate one for the Active record.  ``lifetime="session"`` and
        # no ``ready`` probe match how exec-adopted processes are used.
        synthetic_input = StartInput(
            session_id=session_id,
            command=command,
            cwd=cwd,
            description=resolved_description,
            ready=None,
            lifetime="session",
            extra_env=None,
            shell=None,
            inject_file_guard=False,
        )
        info = Info(
            id=process_id,
            session_id=session_id,
            pid=proc.pid,
            command=command,
            cwd=cwd,
            description=resolved_description,
            ports=(),
            # Adopted processes are already past the ``starting`` phase —
            # we know they are alive (we just checked ``returncode is None``).
            status="running",
            lifetime="session",
            ready=False,
            exit_code=None,
            signal=None,
            output="",
            time=Time(started=now_ms, updated=now_ms, ended=None),
        )
        active = Active(
            info=info,
            proc=proc,
            start_input=synthetic_input,
            adopted=True,
            pattern=None,
            output_bytes=b"",
            publish_handle=None,
            ready_event=asyncio.Event(),
            spawn_started_at=datetime.now(tz=timezone.utc),
        )
        # Insert the record and spawn the same pump tasks ``start`` uses so
        # the adopted child's stdout/stderr keep flowing into the ring
        # buffer (otherwise a chatty background command would block on a
        # full OS pipe once exec stopped reading — 8 KiB on Windows, 64 KiB
        # on Linux).  Skipping the pumps was the original v1 mistake.
        self._register_active(active)
        if proc.stdout is not None:
            asyncio.create_task(
                self._pump_stream(active, proc.stdout),
                name=f"bgp.stdout[{process_id}]",
            )
        if proc.stderr is not None:
            asyncio.create_task(
                self._pump_stream(active, proc.stderr),
                name=f"bgp.stderr[{process_id}]",
            )
        asyncio.create_task(
            self._on_exit(active, proc),
            name=f"bgp.exit[{process_id}]",
        )
        # First publish: subscribers see the adopted record immediately.
        self._publish_now(active)
        return _clone_info(active.info)

    async def shutdown(self) -> None:
        """Terminate everything (idempotent).

        All tracked records are terminated and removed silently (no Deleted
        events fired because the whole daemon is going down). That includes
        records merely being RETAINED post-exit (P7): their pending eviction
        timers are cancelled with them, so no timer outlives the manager.

        Idempotent: calling shutdown twice is a no-op on the second
        call.
        """
        if self._shutdown_called:
            return
        self._shutdown_called = True

        targets = list(self._state.processes.values())
        await asyncio.gather(
            *(
                self._terminate(a, remove=True, silent=True)
                for a in targets
            ),
            return_exceptions=True,
        )
        # ``return_exceptions=True`` means a ``_terminate`` that raised left
        # its record's eviction timer armed, and the ``clear()`` below then
        # drops the record that timer would have been cancelled with — the
        # handle would sit on the loop for the whole retention window,
        # contradicting this method's contract.  Cancelling here closes that
        # path; it is idempotent, so records that terminated cleanly (already
        # cancelled, handle set to ``None``) are unaffected.
        for active in targets:
            handle = active.evict_handle
            if handle is not None:
                handle.cancel()
                active.evict_handle = None
        self._state.processes.clear()

    # =========================================================
    # Internal: launch + spawn + register
    # =========================================================

    async def _launch(
        self, input: StartInput, id: str | None = None
    ) -> Info:
        """Spawn + register + first publish + ready wait. Returns Info."""
        # 1. Pre-spawn validation (synchronous, must not have created
        # any process yet on raise).
        # 2026-07-13: honour caller-selected shell (LLM-facing alias); falls
        # back to :func:`shell.acceptable` when ``input.shell`` is None /
        # ``"auto"`` or when the alias cannot be resolved on this platform.
        chosen_shell = _resolve_shell_alias(input.shell)
        cwd = self._resolve_cwd(input.cwd)
        pattern_compiled = ready_module.compile_pattern(
            input.ready.pattern if input.ready else None
        )
        if input.ready is not None and input.ready.port is not None:
            if await ready_module.connected(input.ready.port):
                raise ReadyPortInUse(
                    f"Ready port is already in use: {input.ready.port}"
                )

        # 2. ID + argv + env
        process_id = id or new_bgp_id()
        argv = shell_module.build_argv(
            input.command, cwd, shell=chosen_shell
        )
        # Resolve the FileGuard guard-token per spawn (State-Truth-First:
        # the native guard starts lazily, so a wiring-time snapshot could
        # be stale). Only injected when the caller explicitly opts in via
        # ``StartInput.inject_file_guard=True`` — LLM tool-call paths set
        # this; internal callers (App Builder, HTTP operator) leave it False
        # so their child processes are not subject to the native hook and
        # will not trigger authorization dialogs for normal file access.
        guard_token: str | None = None
        if input.inject_file_guard and self._guard_token_provider is not None:
            try:
                guard_token = self._guard_token_provider()
            except Exception:  # noqa: BLE001 — never let token lookup block spawn
                guard_token = None
        env = env_strip_module.build_child_env(
            bgp_id=process_id, guard_token=guard_token
        )
        # x86 process escape hatch: when the user enables "Allow 32-bit
        # processes" in Security settings, propagate QAI_GUARD_ALLOW_X86=1 so
        # the native guard64 HookedCreateProcessW does not terminate x86
        # children spawned by background_process.
        if self._allow_x86:
            env["QAI_GUARD_ALLOW_X86"] = "1"
        # Caller-supplied non-secret overlay (e.g. APP_ROOT / PYTHONPATH /
        # QAIRT PATH). Applied AFTER build_child_env so it augments the
        # already credential-stripped baseline. We RE-APPLY the same
        # credential-strip predicate to the overlay keys so the manager's
        # "child env carries no secrets" invariant (ports.py Protocol
        # docstring + AGENTS.md §3.3) is ENFORCED, not merely documented:
        # a buggy/malicious caller cannot smuggle a ``*_TOKEN`` / ``*_SECRET``
        # / ``*_API_KEY`` back in through ``extra_env``. Dropped keys are
        # logged so the misuse is visible.
        if input.extra_env:
            for _k, _v in dict(input.extra_env).items():
                if (
                    _k in env_strip_module.CREDENTIAL_ENV_NAMES
                    or env_strip_module.SENSITIVE_NAME_PATTERN.search(_k)
                ):
                    logger.warning(
                        "background_process.extra_env.credential_dropped",
                        extra={"process_id": process_id, "var": _k},
                    )
                    continue
                # 2026-07-09: PYTHONPATH must MERGE, not overwrite. build_child_env
                # prepends the protected-paths child-hook dir to PYTHONPATH so
                # the child interpreter cannot write the SDK tree (2026-06-16
                # backstop). A caller's extra_env PYTHONPATH (e.g. App Builder's
                # model/pack roots) must be APPENDED after the hook dir, else the
                # hook silently disappears and the child loses protection.
                if _k == "PYTHONPATH" and env.get("PYTHONPATH"):
                    env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + _v
                    continue
                env[_k] = _v

        # 3. Spawn (with appropriate flags). ``start_new_session=True``
        # on POSIX puts the child in its own process group so the kill
        # path can target the whole tree via ``killpg``. On Windows we
        # rely on the Job Object rail.
        creationflags = (
            no_window_creationflags() if sys.platform == "win32" else 0
        )
        start_new_session = sys.platform != "win32"

        # Structured spawn diagnostics.
        #
        # Rationale: the previous ``BGP_SPAWN_DIAG`` line dumped the full argv +
        # a 500-char PATH prefix at INFO on every spawn — ~2 KiB of noise per
        # app launch that flooded operator logs (users reported reading it as
        # "too long, not useful"). Split into two levels:
        #   * INFO — one concise, structured summary so operators can see a
        #     spawn happened at all (bgp id, program basename, argv arity,
        #     resolved port, cwd basename).
        #   * DEBUG — the full argv + PATH prefix, only surfaced when someone
        #     opts into shell/arch spawn debugging via a lowered log level.
        # The spawn-failure path below re-logs the full argv at ERROR when it
        # actually matters (a spawn that crashed needs the raw command).
        _program = Path(argv[0]).name if argv else "?"
        _cwd_short = Path(cwd).name if cwd else ""
        _port_hint = ""
        # Extract the numeric --port arg (if any) so operators can correlate
        # the log line with the port the app was tried on. Best-effort: any
        # parse failure is silently ignored — port is a cosmetic hint here.
        try:
            for i, tok in enumerate(argv):
                if isinstance(tok, str) and tok == "--port" and i + 1 < len(argv):
                    _port_hint = f" port={argv[i + 1]}"
                    break
        except Exception:  # noqa: BLE001 - cosmetic
            pass
        logger.info(
            "bgp.spawn id=%s program=%s argv_len=%d%s cwd=%s",
            process_id, _program, len(argv), _port_hint, _cwd_short,
        )
        logger.debug(
            "bgp.spawn.detail argv=%r cwd=%r PATH_head=%r",
            list(argv), cwd, (env.get("PATH", "") if env else "")[:500],
        )

        # 2026-07-13 (D2-C): sample the wall-clock BEFORE spawn so
        # ``_on_exit`` can pass a ``since`` filter to the native FileGuard
        # denial probe. ``timezone.utc`` matches what ``AuditEntry.
        # occurred_at`` is stored with — a naive value would be
        # interpreted as local time by the SQL adapter and miss recent
        # DENY rows. Captured unconditionally (probe injection is
        # optional; the field on ``Active`` is cheap and lets the manager
        # stay dumb about whether a probe is wired).
        spawn_started_at = datetime.now(tz=timezone.utc)

        try:
            proc = await asyncio.create_subprocess_exec(
                argv[0],
                *argv[1:],
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=start_new_session,
                creationflags=creationflags,
            )
        except OSError as exc:
            # Spawn failure (binary missing, wrong arch, permission denied,
            # bad shell quoting, etc.). No Active was created so there is
            # nothing to roll back. Surface the full command + cwd + PATH at
            # ERROR so operators can debug spawn crashes from the log alone —
            # the INFO/DEBUG split above keeps normal spawns clean but a
            # broken spawn genuinely needs the raw argv to diagnose.
            logger.error(
                "bgp.spawn.failed id=%s exc=%r argv=%r cwd=%r PATH_head=%r",
                process_id,
                exc,
                list(argv),
                cwd,
                (env.get("PATH", "") if env else "")[:500],
            )
            raise

        # 4. Job Object assignment (orphan kill defense-in-depth).
        if proc.pid is not None:
            kill_module.assign_to_job(self._kill_group, proc.pid)

        # 5. Build Active + register.
        active = self._build_active(
            process_id=process_id,
            input=input,
            proc=proc,
            pattern=pattern_compiled,
            cwd=cwd,
            spawn_started_at=spawn_started_at,
        )
        self._register_active(active)

        # 6. Spawn stdout/stderr/exit pumps.
        if proc.stdout is not None:
            asyncio.create_task(
                self._pump_stream(active, proc.stdout),
                name=f"bgp.stdout[{process_id}]",
            )
        if proc.stderr is not None:
            asyncio.create_task(
                self._pump_stream(active, proc.stderr),
                name=f"bgp.stderr[{process_id}]",
            )
        asyncio.create_task(
            self._on_exit(active, proc),
            name=f"bgp.exit[{process_id}]",
        )

        # 7. First publish (immediate, not debounced - lets the UI see
        # the spawn instantly).
        self._publish_now(active)

        # 8. Optional ready wait.
        try:
            if input.ready is not None:
                ready_ok = await self._wait_for_ready(active, input.ready)
                if (
                    not ready_ok
                    and active.info.status == "starting"
                    and not _terminal(active.info.status)
                ):
                    # Ready window elapsed without firing -> upgrade to
                    # ``running`` (wait timeout branch).
                    self._set_status(active, "running")
                    self._publish_now(active)
            else:
                # No ready probe -> the process is considered "running"
                # as soon as the spawn returned (launch default).
                if active.info.status == "starting":
                    self._set_status(active, "running")
                    self._publish_now(active)
            return _clone_info(active.info)
        except Exception:
            # Spawn-time error after Active registration: tear it down
            # so we do not leak a tracked record.
            await self._rollback(active)
            raise

    def _build_active(
        self,
        *,
        process_id: str,
        input: StartInput,
        proc: asyncio.subprocess.Process,
        pattern: re.Pattern[str] | None,
        cwd: str,
        spawn_started_at: datetime,
    ) -> Active:
        """Build the per-process Active record + initial Info."""
        now_ms = self._now_ms()
        info = Info(
            id=process_id,
            session_id=input.session_id,
            pid=proc.pid,
            command=input.command,
            cwd=cwd,
            # Truth-first label fallback (P2 §2): a caller that omitted the
            # description still gets a non-empty label — the command line
            # itself. ``start_input`` below keeps the caller's ORIGINAL value
            # (``None`` stays ``None``): it records intent, and ``restart``
            # re-derives the identical fallback from the same command.
            description=_derive_description(
                input.description, input.command
            ),
            ports=(),
            status="starting",
            lifetime=input.lifetime,
            ready=False,
            exit_code=None,
            signal=None,
            output="",
            time=Time(started=now_ms, updated=now_ms, ended=None),
        )
        return Active(
            info=info,
            proc=proc,
            start_input=input,
            pattern=pattern,
            output_bytes=b"",
            publish_handle=None,
            ready_event=asyncio.Event(),
            spawn_started_at=spawn_started_at,
        )

    def _register_active(self, active: Active) -> None:
        """Insert active into the state map."""
        self._state.processes[active.info.id] = active

    def _processes_for(self, active: Active) -> dict[str, Active]:
        """Return the dict that currently holds ``active``."""
        del active
        return self._state.processes

    def _find(self, process_id: str) -> Active | None:
        """Look up an Active by id."""
        return self._state.processes.get(process_id)

    # =========================================================
    # Internal: output pump + pattern match + publish debounce
    # =========================================================

    async def _pump_stream(
        self,
        active: Active,
        stream: asyncio.StreamReader,
    ) -> None:
        """Drain a child stdout/stderr stream into the ring buffer.

        Loops on ``stream.read(64 KiB)`` rather than ``readline`` so
        binary-ish output (ANSI escapes, partial multi-byte chunks)
        never blocks. Stops cleanly on EOF (child closed pipe / exited).
        """
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                self._append_output(active, chunk)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Cancellation or any IO error - exit silently; _on_exit
            # will be the one to flip status to terminal.
            return

    def _append_output(self, active: Active, chunk: bytes) -> None:
        """Append chunk + maybe-mark-ready + schedule debounced publish.

        A RETAINED terminal record (P7: ``evict_handle`` armed, ``disposed``
        deliberately still ``False`` so the record stays queryable) is closed
        to further output.  ``_on_exit`` does not await the stdout/stderr
        pumps, so a chunk still sitting in the pipe can land after the exit
        was published; without this guard it would re-arm the publish timer
        that ``_exited`` just cancelled, and subscribers would keep receiving
        ``updated`` events for a process that has already finished.  The
        output buffer is bounded either way, so this is about not emitting a
        contradictory event stream rather than about memory.
        """
        if not chunk or active.disposed or active.evict_handle is not None:
            return
        # 2026-07-09 — strip + audit child protected-deny markers (parity with
        # exec). The D.1 child-hook writes ``[[QAI_PROTECTED_DENY]] {...}`` lines
        # to stderr when a background child is blocked from writing a protected
        # (SDK) path; remove them from the ring buffer so the model/operator
        # never sees the internal protocol, and dispatch each to the audit
        # callback. Best-effort per-chunk: a marker split across read chunks may
        # slip through un-stripped, but that is cosmetic — the WRITE was already
        # blocked by the hook; this only governs the marker line's visibility.
        try:
            from qai.platform.child_process_deny_audit import (
                notify_child_protected_deny,
                parse_and_strip_deny_markers,
            )

            if b"[[QAI_PROTECTED_DENY]]" in chunk:
                text = chunk.decode("utf-8", errors="replace")
                clean, denies = parse_and_strip_deny_markers(text)
                if denies:
                    notify_child_protected_deny(denies)
                    chunk = clean.encode("utf-8")
        except Exception:  # noqa: BLE001 — marker handling must not break pump
            pass
        if not chunk:
            return
        active.output_bytes = buffer_module.append_clamped(
            active.output_bytes, chunk, cap=OUTPUT_BYTE_CAP
        )
        now_ms = self._now_ms()
        new_output = buffer_module.decode_lossy(active.output_bytes)
        active.info = dataclasses.replace(
            active.info,
            output=new_output,
            time=dataclasses.replace(active.info.time, updated=now_ms),
        )
        # Pattern hit -> ready signal (append).
        if (
            active.pattern is not None
            and active.pattern.search(new_output)
            and not active.ready_event.is_set()
        ):
            self._mark_ready(active)
        self._schedule_publish(active)

    def _schedule_publish(self, active: Active) -> None:
        """Coalesce publishes to PUBLISH_DEBOUNCE_S."""
        if active.disposed or active.publish_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (test tear-down) -- skip.
            return

        def _fire() -> None:
            active.publish_handle = None
            if active.disposed:
                return
            self._publish_now(active)

        active.publish_handle = loop.call_later(PUBLISH_DEBOUNCE_S, _fire)

    def _publish_now(self, active: Active) -> None:
        """Publish a BackgroundProcessUpdated synchronously (best-effort)."""
        if active.disposed:
            return
        scope = self._event_scope(active)
        event = BackgroundProcessUpdated(
            info=_clone_info(active.info),
            scope=scope,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._events.publish(event),
            name=f"bgp.publish[{active.info.id}]",
        )

    # =========================================================
    # Internal: terminal-record retention (P7)
    # =========================================================

    def _schedule_eviction(self, active: Active) -> None:
        """Keep a settled record queryable for :data:`_RETENTION_S`, then drop it.

        Idempotent — a record that already has a timer keeps its ORIGINAL
        deadline. That matters for the ``stop``-after-natural-exit sequence:
        re-arming would let repeated observation of a dead process extend its
        retention indefinitely, when the window should count from the moment
        the process actually settled.

        Retention holds the RECORD, not the process. The child is already
        reaped; ``get`` / ``logs`` / ``list`` answer from the same in-memory
        ``Info`` they always did, so the window's only effect is that "the
        process just finished and the user reloaded the page" reads the
        authoritative terminal state instead of a 404.

        Memory: a retained record costs its metadata plus its output ring
        buffer, and that buffer is capped at :data:`OUTPUT_BYTE_CAP`
        (200 KiB) by the single writer that ever grows it
        (``_append_output`` → ``buffer.append_clamped``), so the ceiling is
        ~200 KiB per record settled within any one window. The tail is kept
        intact rather than trimmed on eviction-pending records: serving
        ``logs`` for a just-finished process is the entire point of the
        window. This is strictly TIGHTER than before — a naturally-exited
        record was previously never retired at all, so a long session
        accumulated one such record per command, forever.

        With no running loop (synchronous tear-down) there is nobody to fire
        a timer, so the record is dropped at once rather than leaked.
        """
        if active.evict_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._evict(active, silent=True)
            return
        active.evict_handle = loop.call_later(
            _RETENTION_S, self._evict, active
        )

    def _evict(self, active: Active, *, silent: bool = False) -> None:
        """Drop ``active`` from the task map; announce it unless ``silent``.

        The single removal path — every caller that retires a record goes
        through here, so the pending retention timer can never outlive the
        record it was going to evict (a ``restart`` reusing the id would
        otherwise have its fresh record deleted by the old timer).

        Ownership re-check: after a ``restart`` the id maps to a DIFFERENT
        ``Active``, and evicting on that id would delete a live process.
        """
        handle, active.evict_handle = active.evict_handle, None
        if handle is not None:
            handle.cancel()
        bucket = self._processes_for(active)
        if bucket.get(active.info.id) is not active:
            return
        bucket.pop(active.info.id, None)
        active.disposed = True
        if not silent:
            self._publish_deleted(active)

    def _publish_deleted(self, active: Active) -> None:
        """Emit ``BackgroundProcessDeleted`` for a retired record (best-effort)."""
        scope = self._event_scope(active)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._events.publish(
                BackgroundProcessDeleted(
                    session_id=active.info.session_id,
                    process_id=active.info.id,
                    scope=scope,
                )
            ),
            name=f"bgp.deleted[{active.info.id}]",
        )

    # =========================================================
    # Internal: ready signalling
    # =========================================================

    async def _wait_for_ready(self, active: Active, ready: Ready) -> bool:
        """Race ready_event vs port-poll vs timeout."""
        timeout_s = (
            (ready.timeout / 1000.0)
            if ready.timeout is not None
            else READY_DEFAULT_S
        )
        # Fast path: pattern already matches current buffer
        # (wait fast-path).
        if active.pattern is not None and active.pattern.search(
            active.info.output
        ):
            self._mark_ready(active)
            return True
        # 2026-07-09 — pause the readiness window while the child is BLOCKED on
        # a native FileGuard ASK (parity with exec's ask_pending_probe). Without
        # this, the user deciding on a native file-access dialog during startup
        # could burn the whole ready timeout and mis-report the process. When a
        # native ASK is pending on the child pid, re-arm the wait for another
        # window instead of giving up. Orphan-safe: without a pending ASK (or no
        # probe / probe error) the ordinary timeout stands. Bounded re-arms
        # guard against an unexpected always-true probe.
        max_rearms = 240  # safety ceiling on consecutive ASK-pause re-arms
        while True:
            ok = await ready_module.wait_for_ready(
                port=ready.port,
                timeout_s=timeout_s,
                is_terminal=lambda: active.info.status in TERMINAL_STATUSES,
                ready_event=active.ready_event,
            )
            if ok or active.info.status in TERMINAL_STATUSES:
                return ok
            if max_rearms <= 0:
                return ok
            if not self._ask_pending_on_child(active):
                return ok
            max_rearms -= 1  # native ASK pending → pause (re-arm) the window

    def _ask_pending_on_child(self, active: Active) -> bool:
        """Best-effort: is a native FileGuard ASK pending on this child's pid?

        Uses the injected ``ask_pending_probe``. Any error / missing probe /
        missing pid → ``False`` (never stall the readiness timeout on
        uncertainty; matches exec's conservative degradation).
        """
        probe = self._ask_pending_probe
        pid = active.info.pid
        if probe is None or pid is None:
            return False
        try:
            return bool(probe(pid))
        except Exception:  # noqa: BLE001 — probe failure → do not pause
            return False

    def _mark_ready(self, active: Active) -> None:
        """Status -> ready (idempotent)."""
        if active.info.ready:
            return
        if active.info.status in TERMINAL_STATUSES:
            return
        active.info = dataclasses.replace(
            active.info,
            status="ready",
            ready=True,
            time=dataclasses.replace(
                active.info.time, updated=self._now_ms()
            ),
        )
        if not active.ready_event.is_set():
            active.ready_event.set()
        self._publish_now(active)

    # =========================================================
    # Internal: exit observation
    # =========================================================

    async def _on_exit(
        self,
        active: Active,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Wait for the child + transition to terminal status.

        Iron-rule 1: this is the *single* truth source for the
        ``exited`` / ``failed`` / ``stopped`` transition. We never set
        those statuses from a soft probe.
        """
        try:
            code = await proc.wait()
        except Exception:  # noqa: BLE001
            # asyncio bookkeeping error - treat as terminal failure.
            code = -1

        # Re-check the active record is still ours (it could have been
        # replaced by a restart() that reused the id).
        bucket = self._processes_for(active)
        if active.disposed or bucket.get(active.info.id) is not active:
            return

        signal_name: str | None = None
        exit_code: int | None = code
        if sys.platform != "win32" and code is not None and code < 0:
            # POSIX convention: ``Popen.returncode == -N`` means signal N.
            signal_name = _signal_name_from_negative(code)
            exit_code = None

        self._exited(active, exit_code, signal_name)

        # 2026-07-13 (D2-C): query native FileGuard denials attributable
        # to this subprocess tree and store the resulting diagnostic note
        # on ``Info.exit_diagnostics``. Rendered by the chat tool result
        # renderer (D2-D) so the LLM sees an authoritative "blocked by
        # FileGuard" cause instead of only a raw ``mv: Permission
        # denied`` and does not retry via elevation / alternate tools.
        #
        # Fail-open (AGENTS.md §5 铁律5): four defense lines — probe
        # absent, ``exit_code == 0`` (clean exit means no denial worth
        # attributing), pid missing (spawn never yielded one), or probe
        # raising — all keep ``exit_diagnostics`` empty so the LLM still
        # sees the raw stdout/stderr as before. When the probe returns
        # an empty string the field also stays empty (D2-A's audit query
        # returns ``""`` when no rows match — no falsy stringification
        # of a bogus note).
        await self._maybe_populate_exit_diagnostics(active)

    async def _maybe_populate_exit_diagnostics(self, active: Active) -> None:
        """Best-effort: fill ``Info.exit_diagnostics`` from the audit probe.

        Called from :meth:`_on_exit` after the terminal transition. Never
        raises: any audit-side failure falls through to a no-op so the
        exit path (and its ``BackgroundProcessUpdated`` publish) stays
        robust. When the field is updated a second publish fires so
        subscribers (SSE / sidebar / logs handler) see the new value —
        the first publish inside :meth:`_exited` already went out with
        the empty default.
        """
        probe = self._native_denial_probe
        if probe is None:
            return
        # Only attribute denials to a non-zero exit. A clean exit means
        # the child completed without a fatal syscall denial; any older
        # DENY row on unrelated pids in the same process tree is not
        # this exit's story to tell.
        if active.info.exit_code is None or active.info.exit_code == 0:
            return
        if active.info.pid is None:
            return
        if active.spawn_started_at is None:
            return
        try:
            note = await probe(active.info.pid, active.spawn_started_at)
        except Exception:  # noqa: BLE001 — never break exit path on audit failure
            note = ""
        if not note:
            return
        # Re-check ownership: the tracked record could have been
        # displaced (restart with reused id) while the probe was
        # awaiting — do not overwrite a foreign Info.
        bucket = self._processes_for(active)
        if active.disposed or bucket.get(active.info.id) is not active:
            return
        active.info = dataclasses.replace(
            active.info,
            exit_diagnostics=note,
        )
        self._publish_now(active)

    def _exited(
        self,
        active: Active,
        exit_code: int | None,
        signal: str | None,
    ) -> None:
        """Apply terminal status (exited transition)."""
        if active.disposed or active.info.status in TERMINAL_STATUSES:
            return

        # Cancel any pending timers / tasks.
        if active.publish_handle is not None:
            active.publish_handle.cancel()
            active.publish_handle = None

        was_stopping = active.info.status == "stopping"
        if was_stopping:
            new_status: Status = "stopped"
        elif exit_code == 0:
            new_status = "exited"
        else:
            new_status = "failed"

        now_ms = self._now_ms()
        active.info = dataclasses.replace(
            active.info,
            status=new_status,
            ports=(),
            # Terminal 状态（exited/failed/stopped）一律不再 ready：进程一旦退出，
            # ready 语义应为 False（exited 转换会清空 ready）。
            ready=False,
            exit_code=exit_code,
            signal=signal,
            time=dataclasses.replace(
                active.info.time,
                updated=now_ms,
                ended=now_ms,
            ),
        )
        # Unblock any waiter (wait_for_ready returns False because
        # is_terminal() now true).
        if not active.ready_event.is_set():
            active.ready_event.set()
        self._publish_now(active)
        # P7: the record is now terminal. Retain it for the query window,
        # then retire it (emitting ``Deleted``). Before 2026-08-09 a
        # naturally-exited process was never retired at all — it sat in the
        # map for the whole session, so a long-lived daemon accumulated one
        # record + its 200 KiB output buffer per command ever backgrounded.
        #
        # Armed unconditionally, including on the ``_terminate`` path that is
        # about to drop the record: ``disposed`` is not yet set when
        # ``_terminate`` synthesises this terminal state, so there is nothing
        # to test here.  ``_evict`` cancels the handle it finds, so the
        # short-lived timer on that path is retired with the record — the
        # ownership rule is simply "``_evict`` owns cancellation", and
        # ``_schedule_eviction`` itself replaces any handle already armed.
        self._schedule_eviction(active)

    # =========================================================
    # Internal: terminate / stop / rollback
    # =========================================================

    async def _terminate(
        self,
        active: Active,
        *,
        remove: bool,
        silent: bool,
        retain: bool = False,
    ) -> None:
        """Kill the process tree + optionally retire the record.

        ``retain`` (P7) keeps the settled record queryable for
        :data:`_RETENTION_S` instead of dropping it here; the
        ``Deleted`` event then fires when the window expires. It is for
        single-process ``stop``, where the caller is likely to ask about the
        outcome it just caused. Bulk / lifecycle teardown (``stop_session``,
        ``shutdown``, and ``restart``'s id-reuse) passes it False: those
        callers are retiring the id itself, so keeping the record would leave
        rows behind for a session that no longer exists — or, for ``restart``,
        collide with the fresh record about to claim the same id.
        """
        if active.info.status not in TERMINAL_STATUSES:
            self._set_status(active, "stopping")
            self._publish_now(active)

            pid = active.info.pid
            if pid is not None:
                try:
                    await kill_module.kill_session(
                        pid=pid, proc=active.proc
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "_terminate: kill raised pid=%s",
                        pid,
                        exc_info=True,
                    )
            # Ensure exit observer flipped us terminal; if the child
            # was already gone, _on_exit may have already run.
            if active.info.status not in TERMINAL_STATUSES:
                # Defensive: synthesise terminal state.
                self._exited(active, None, None)

        # Cancel any pending publish (defensive - _exited already did).
        if active.publish_handle is not None:
            active.publish_handle.cancel()
            active.publish_handle = None

        if not remove:
            active.disposed = True
            return
        if retain:
            # ``_exited`` already armed the window; this only guarantees one
            # exists for a record that reached its terminal status by some
            # other route. ``disposed`` deliberately stays False — a retained
            # record is still live STATE (queryable, still publishable), and
            # ``_evict`` is what marks it gone.
            self._schedule_eviction(active)
            return
        self._evict(active, silent=silent)

    async def _rollback(self, active: Active) -> None:
        """Spawn-time error cleanup. Best-effort kill + remove."""
        # No retention on the rollback path: the spawn FAILED, so there is no
        # terminal outcome worth keeping — nothing ever observed this id, and
        # ``start`` is raising rather than returning it.
        active.disposed = True
        if active.info.pid is not None:
            try:
                await kill_module.kill_session(
                    pid=active.info.pid, proc=active.proc
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "_rollback: kill_session raised", exc_info=True
                )
        self._evict(active, silent=True)

    # =========================================================
    # Internal: helpers
    # =========================================================

    def _resolve_cwd(self, cwd: str | None) -> str:
        """Resolve cwd relative to data_root (StateTruth iron-rule 4)."""
        if cwd is None or not cwd.strip():
            return str(self._data_root)
        candidate = Path(cwd)
        if not candidate.is_absolute():
            candidate = self._data_root / candidate
        return str(candidate.resolve())

    def _event_scope(self, active: Active) -> str:
        """Event scope label for SSE filtering (design doc 3.3)."""
        del active
        return "global"

    def _set_status(self, active: Active, status: Status) -> None:
        """Apply a non-terminal status transition + bump updated."""
        if active.info.status == status:
            return
        if active.info.status in TERMINAL_STATUSES:
            return
        active.info = dataclasses.replace(
            active.info,
            status=status,
            time=dataclasses.replace(
                active.info.time, updated=self._now_ms()
            ),
        )

    def _now_ms(self) -> int:
        """Current epoch milliseconds (injectable clock)."""
        return int(self._clock() * 1000)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _terminal(status: Status) -> bool:
    """``True`` iff ``status`` is one of the terminal values."""
    return status in TERMINAL_STATUSES


def _derive_description(
    description: str | None, command: str
) -> str | None:
    """Return ``description``, or the command line when it is absent.

    Truth-first fallback (P2 §2): a tracked record's label is what the
    UI shows in the sidebar and what concurrent-completion delivery uses
    to tell one job's result from another's, so it must not depend on
    the caller having bothered. The command line is the least
    interpretive stand-in available — it is literally what is running.

    Truncation keeps the head (the program and its leading arguments,
    where the identifying information is) and marks the cut with
    ``"..."`` so a clipped label never reads as a complete command.

    A caller-supplied description is returned verbatim, including
    over-long ones: the cap exists to keep a machine-derived stand-in
    label-sized, not to edit what a caller deliberately wrote.
    Whitespace-only is treated as absent (it would render as a blank
    label, which is the failure mode this exists to prevent), and
    ``None`` is returned when the command is blank too, since an empty
    string in the field is worse than an honest null.
    """
    if description is not None and description.strip():
        return description
    fallback = command.strip()
    if not fallback:
        return None
    if len(fallback) <= DESCRIPTION_FALLBACK_MAX_CHARS:
        return fallback
    return fallback[:DESCRIPTION_FALLBACK_TRUNCATED_CHARS] + "..."


def _clone_info(info: Info) -> Info:
    """Return a fresh ``Info`` snapshot.

    Frozen dataclasses are already immutable; this helper exists so
    the call site reads as "I'm handing a snapshot to a subscriber",
    keeping subscriber-mutation expectations explicit. Calls
    :func:`dataclasses.replace` with no overrides to materialise a
    new instance.
    """
    return dataclasses.replace(info)


def _signal_name_from_negative(code: int) -> str:
    """Map ``Popen.returncode == -N`` to a signal name (POSIX only).

    Falls back to ``"SIG{N}"`` if the platform's ``signal`` module
    does not enumerate the signal. Returns the bare name (no leading
    SIG strip) so callers can pass it straight into ``Info.signal``.
    """
    n = -code
    try:
        import signal

        for attr in dir(signal):
            if not attr.startswith("SIG") or attr.startswith("SIG_"):
                continue
            try:
                if int(getattr(signal, attr)) == n:
                    return attr
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001 - signal module quirks; fall through
        pass
    return f"SIG{n}"


__all__ = [
    "Active",
    "DESCRIPTION_FALLBACK_MAX_CHARS",
    "DESCRIPTION_FALLBACK_TRUNCATED_CHARS",
    "KILL_GRACE_S",
    "OUTPUT_BYTE_CAP",
    "PUBLISH_DEBOUNCE_S",
    "READY_DEFAULT_S",
    "State",
    "SubprocessBackgroundProcessManager",
]
