# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sticky worker host — long-running multi-model worker manager (PR-301).

Reimplements the legacy
``backend/app_builder/runners/sticky_worker.py`` host inside the
:mod:`qai.app_builder.infrastructure.sticky_worker` package so the
asyncio process management is owned by the App Builder bounded
context (no ``backend.*`` import).

Lifecycle (mirrors SSOT
``docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md`` §1.1):

::

    spawn → idle (no model)
          → load_model → ready ⇄ busy → release → ready / idle
          → shutdown
          (any IO failure / process exit → dead)

Concurrency
-----------

* At most one host instance is intended to be alive at a time; the
  legacy ``runner._npu_lock`` (asyncio.Lock) provides FIFO inference
  ordering on top, but is **not** owned by this module — the use case
  layer (PR-302's ``RunAppUseCase`` integration) acquires it.
* Inside the host, an internal ``_op_lock`` serialises protocol
  ops (load / run / cancel / release / ping) so the stdin/stdout
  framing never interleaves.

What's *not* in PR-301
----------------------

* runner_protocol v3.1 payload-level event types (``status / result /
  done / progress / metrics``) — surfaced through
  :class:`WorkerEvent` envelopes for now; PR-302 specialises them.
* A real ``_default_resolver`` mapping ``(model, variant) →
  ProcessExecutionRequest``. The host accepts a ``BootstrapSpec``
  (argv + env + cwd) injected by the caller; PR-302 introduces the
  resolver that consumes ``manifest.json`` v1.
* Lifespan warm-up registration in ``apps/api/lifespan.py`` — that
  file is owned by the I1 lane; this PR's manifest §10 lists the
  required hook.

Tests
-----

Unit tests in ``tests/unit/qai/app_builder/test_sticky_worker_*.py``
cover the protocol codec without spawning a subprocess. Integration
tests in ``tests/integration/app_builder/test_sticky_worker_host.py``
spawn a tiny in-tree Python script that speaks the protocol so the
real :class:`asyncio.subprocess` IO is exercised end-to-end on the
ARM64 venv.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from qai.platform.process import ProcessKillGroup

from .models import (
    LoadedModelEntry,
    LoadModelRequest,
    RunRequest,
    WorkerEvent,
)
from .protocol import BootstrapProtocol

if TYPE_CHECKING:
    from qai.platform.events import EventBus

__all__ = [
    "BootstrapSpec",
    "StickyWorkerHost",
    "StickyWorkerSpawnError",
]

logger = logging.getLogger(__name__)


# Well-known Windows NTSTATUS / exit codes a crashing worker tends to exit
# with. Decoding them inline makes the ``_mark_dead`` log self-diagnosing so a
# later triage pass does not have to hand-convert ``returncode=-1073741819``
# into an access violation. Mirrors ``apps.cli.serve._WINDOWS_STATUS_NAMES``
# (kept as a separate small copy to avoid an infra→cli import).
_WORKER_STATUS_NAMES: dict[int, str] = {
    0xFFFFFFFF: "TERMINATED/-1 (native exit(-1)/abort() - e.g. onnxruntime / "
    "QNN model teardown, or an external TerminateProcess)",
    0xC0000005: "ACCESS_VIOLATION (native segfault - use-after-free / freed "
    "model pointer)",
    0xC0000374: "HEAP_CORRUPTION (double-free / buffer overrun in a native "
    "extension)",
    0xC0000409: "STACK_BUFFER_OVERRUN / __fastfail (native abort())",
    0xC00000FD: "STACK_OVERFLOW",
    0xC000013A: "CONTROL_C_EXIT",
}


def _describe_returncode(rc: int | None) -> str:
    """Human-readable label for a worker subprocess return code.

    Best-effort and never raises. On POSIX a negative value is a signal
    kill (``-N``); on Windows the raw 32-bit code is decoded against the
    common NTSTATUS names above.
    """

    if rc is None:
        return "returncode=None (still running or never reaped)"
    try:
        if rc < 0:
            return f"returncode={rc} (killed by signal {-rc})"
        as_u32 = rc & 0xFFFFFFFF
        name = _WORKER_STATUS_NAMES.get(as_u32)
        if name is not None:
            return f"returncode={rc} (0x{as_u32:08X}, {name})"
        if as_u32 >= 0xC0000000:
            return (
                f"returncode={rc} (0x{as_u32:08X}, unrecognised NTSTATUS)"
            )
        return f"returncode={rc} (0x{as_u32:08X})"
    except Exception:  # pragma: no cover - diagnostics must never raise
        return f"returncode={rc}"


# ---------------------------------------------------------------------------
# Configuration helpers (mirror legacy QAI_STICKY_* env-var contract)
# ---------------------------------------------------------------------------
_DEFAULT_IDLE_RELEASE_S: Final[int] = 600
_DEFAULT_HEALTH_INTERVAL_S: Final[float] = 60.0
_DEFAULT_FORCE_KILL_S: Final[float] = 5.0
_DEFAULT_WORKER_READY_TIMEOUT_S: Final[float] = 30.0
_DEFAULT_LOAD_MODEL_TIMEOUT_S: Final[float] = 120.0
_DEFAULT_PING_BUDGET_BASE_S: Final[float] = 10.0
_DEFAULT_PING_BUDGET_PER_RELEASE_S: Final[float] = 5.0

# Auto-respawn defaults: the host will automatically attempt to respawn the
# worker up to ``_DEFAULT_MAX_RESPAWN_ATTEMPTS`` times within a rolling window
# of ``_DEFAULT_RESPAWN_WINDOW_S`` seconds. If the budget is exhausted the
# worker stays dead until manually restarted (preventing infinite crash loops).
_DEFAULT_RESPAWN_DELAY_S: Final[float] = 2.0
_DEFAULT_MAX_RESPAWN_ATTEMPTS: Final[int] = 3
_DEFAULT_RESPAWN_WINDOW_S: Final[float] = 300.0  # 5 minutes


def _multimodel_enabled_default() -> bool:
    """Read ``QAI_STICKY_MULTIMODEL`` env-var (default: enabled).

    Public so DI / tests can mirror the same parsing rule.
    """
    raw = (os.environ.get("QAI_STICKY_MULTIMODEL") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _idle_release_seconds_default() -> int:
    raw = os.environ.get("QAI_STICKY_IDLE_RELEASE_S", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_IDLE_RELEASE_S


# ---------------------------------------------------------------------------
# BootstrapSpec — caller-supplied "how to spawn the worker subprocess"
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class BootstrapSpec:
    """Description of the bootstrap subprocess to spawn.

    Caller-injected so the host stays free of QAIRT / pack-resolver
    details. PR-302 introduces a resolver that builds this from the
    manifest schema; PR-301 tests build it directly with a small
    Python ``-c`` script that speaks the protocol.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    # Phase 1 T1/T2: optional random trust token. When set, _default_spawn
    # injects it as ``QAI_FILEGUARD_TRUST_TOKEN`` (and a companion
    # ``QAI_FILEGUARD_KIND=trusted_infra`` marker) into the child env so the
    # native ``guard64.dll`` classifies the subprocess as TrustedInfra and
    # skips the ASK pipeline on undetermined paths. Field is tail-appended
    # with a ``None`` default so existing callers (Settings S0-S7 field-lock
    # style compat) never have to pass it — the launcher wires it from the
    # NativeFileGuardPort.get_trusted_infra_token() when the hook is on.
    trust_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple of str")
        for i, item in enumerate(self.argv):
            if not isinstance(item, str) or not item:
                raise ValueError(f"argv[{i}] must be a non-empty str")
        if self.cwd is not None and not isinstance(self.cwd, Path):
            raise ValueError("cwd must be Path or None")
        if self.env is not None and not isinstance(self.env, Mapping):
            raise ValueError("env must be Mapping or None")
        if self.trust_token is not None and not isinstance(
            self.trust_token, str
        ):
            raise ValueError("trust_token must be str or None")


SpawnFn = Callable[[BootstrapSpec], Awaitable["asyncio.subprocess.Process"]]
"""Type alias for the function the host uses to spawn a subprocess.

Defaulted by :func:`_default_spawn` (uses
:func:`asyncio.create_subprocess_exec`); tests inject an in-process
fake so they can drive the protocol without a real fork.
"""


async def _default_spawn(spec: BootstrapSpec) -> asyncio.subprocess.Process:
    """Spawn the worker via :func:`asyncio.create_subprocess_exec`.

    Always opens stdin/stdout/stderr as pipes (the host owns all three).

    PYTHONPATH augmentation
    -----------------------
    The runner subprocess needs ``shared/`` (containing ``runner_protocol``,
    ``audio_io``, etc.) on its import path.  The layout is::

        <pack_root>/
            shared/          ← runner_protocol.py lives here
            models/<id>/
                runner.py    ← does ``from runner_protocol import emit``

    We also add the runner's own directory so sibling imports work.

    The bootstrap script (``_runner_bootstrap.py``) manipulates ``sys.path``
    internally as well, but setting PYTHONPATH in the env is defence-in-depth
    and ensures one-shot runners can always find shared modules.
    """
    env: dict[str, str] | None
    if spec.env is None:
        env = dict(os.environ)
    else:
        env = dict(spec.env)

    # Resolve shared/ and runner dir from the argv or cwd.
    # Convention: cwd is the pack root (factory/app_builder/) OR
    # we can infer shared/ from argv (the bootstrap script path).
    #
    # IMPORTANT (runner_protocol shadowing fix): we must NOT put the
    # bootstrap script's own directory (``src/qai/app_builder/infrastructure``)
    # on the worker's import path. That directory contains the host-side
    # ``runner_protocol`` *package* (an NDJSON decoder with no ``emit`` /
    # ``read_request`` functions). If it precedes the Pack ``shared/`` dir,
    # the resident worker's ``from runner_protocol import emit`` resolves to
    # the wrong module and every persistent-mode model load fails with
    # ``cannot import name 'emit' from 'runner_protocol'``. The bootstrap
    # script is always passed by absolute path in ``argv`` so it never needs
    # its own directory on ``sys.path``. We therefore only ever add the Pack
    # ``shared/`` dir (and, in one-shot mode, the runner's own dir).
    extra_paths: list[str] = []

    # The bootstrap script's own directory — used ONLY to exclude it from the
    # import path (see note above), never to add it.
    _bootstrap_own_dir = os.path.normpath(
        str(Path(__file__).resolve().parent.parent)  # infrastructure/
    )

    # If cwd is provided, shared/ is <cwd>/shared
    if spec.cwd is not None:
        shared_candidate = str(spec.cwd / "shared")
        if os.path.isdir(shared_candidate):
            extra_paths.append(shared_candidate)
    else:
        # Infer from a .py file in argv. In one-shot mode argv carries the
        # per-model ``runner.py`` (whose sibling chain has a ``shared/``); in
        # persistent mode argv carries ``_runner_bootstrap.py`` (whose dir is
        # the infrastructure pkg — deliberately skipped). We add the script's
        # own dir only when it is NOT the bootstrap/infrastructure dir, then
        # the discovered Pack ``shared/``.
        for arg in spec.argv:
            if arg.endswith(".py") and os.path.isfile(arg):
                script_dir = os.path.normpath(
                    os.path.dirname(os.path.abspath(arg))
                )
                if script_dir != _bootstrap_own_dir:
                    extra_paths.append(script_dir)
                # Try <script_dir>/../shared and <script_dir>/../../shared
                for levels in (1, 2, 3):
                    candidate = os.path.normpath(
                        os.path.join(script_dir, *([os.pardir] * levels), "shared")
                    )
                    if os.path.isdir(candidate):
                        extra_paths.append(candidate)
                        break
                break

    # Always rebuild PYTHONPATH (even when ``extra_paths`` is empty) so the
    # ``_bootstrap_own_dir`` filter below runs against any inherited
    # PYTHONPATH. The host process commonly runs with ``PYTHONPATH=src;.``
    # which makes ``qai.app_builder.infrastructure`` (carrying the wrong
    # ``runner_protocol``) reachable; ``build_persistent_bootstrap_spec``
    # also prepends the Pack ``shared/`` here — we must guarantee the
    # infrastructure dir never ends up ahead of (or alongside) it on the
    # worker import path.
    existing = env.get("PYTHONPATH", "")
    if extra_paths or existing:
        separator = os.pathsep
        # Deduplicate while preserving order
        seen: set[str] = set()
        parts: list[str] = []
        for p in extra_paths:
            normed = os.path.normpath(p)
            if normed == _bootstrap_own_dir:
                continue
            if normed not in seen:
                seen.add(normed)
                parts.append(normed)
        if existing:
            for p in existing.split(separator):
                if not p:
                    continue
                normed = os.path.normpath(p)
                # Defence-in-depth: never let the host-side infrastructure dir
                # (carrying the wrong ``runner_protocol`` package) leak onto
                # the worker import path from an inherited PYTHONPATH.
                if normed == _bootstrap_own_dir:
                    continue
                if normed not in seen:
                    seen.add(normed)
                    parts.append(normed)
        env["PYTHONPATH"] = separator.join(parts)

    # Phase 1 T1/T2: TrustedInfra env markers for host-spawned subprocesses.
    # When the caller threaded a trust_token through the BootstrapSpec, inject
    # it as QAI_FILEGUARD_TRUST_TOKEN so the native guard64.dll (loaded via
    # CreateProcessW hook) classifies the child as TrustedInfra and short-
    # circuits the ASK pipeline on undetermined paths. The companion
    # QAI_FILEGUARD_KIND marker records intent for diagnostics. Absence of
    # trust_token (native hook disabled or not started) leaves env untouched
    # so behaviour is byte-for-byte identical to before Phase 1.
    if spec.trust_token:
        env["QAI_FILEGUARD_TRUST_TOKEN"] = spec.trust_token
        env["QAI_FILEGUARD_KIND"] = "trusted_infra"

    cwd = str(spec.cwd) if spec.cwd is not None else None
    # Windows: create the worker with ``CREATE_BREAKAWAY_FROM_JOB`` so it can
    # cleanly leave any *outer* Job Object the API process may already belong
    # to (service hosts / some launchers put us in one). This is the spawn-side
    # complement to ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`` on our own
    # KILL_ON_JOB_CLOSE job (see ``ProcessKillGroup``): together they guarantee
    # (a) ``AssignProcessToJobObject`` into OUR job succeeds instead of failing
    # with nested-job semantics, and (b) the worker's lifetime is coupled ONLY
    # to our job, never to an outer job whose close would otherwise reap the
    # worker (and confuse crash triage).
    #
    # Platform detection uses ``os.name`` (not ``sys.platform``) because ``os``
    # is already heavily used in this function and is guaranteed present — this
    # avoids any chance of a ``NameError`` on the spawn path aborting worker
    # startup. On POSIX we pass no ``creationflags`` at all (the kwarg is a
    # Windows-only concept) so the call is identical to before on Linux/macOS.
    kwargs: dict[str, Any] = dict(
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if os.name == "nt":
        # subprocess.CREATE_BREAKAWAY_FROM_JOB == 0x01000000. Best-effort: if
        # the outer job forbids breakaway the OS ignores the flag and the spawn
        # still succeeds, so this never regresses startup.
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x0100_0000
        )
    return await asyncio.create_subprocess_exec(*spec.argv, **kwargs)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class StickyWorkerSpawnError(RuntimeError):
    """Raised when the host fails to spawn / handshake with a worker."""


class _WorkerDead(RuntimeError):
    """Internal sentinel — process died mid-op; surfaced as ``state="dead"``."""


# ---------------------------------------------------------------------------
# StickyWorkerHost
# ---------------------------------------------------------------------------
class StickyWorkerHost:
    """Asyncio-side manager for a long-running App Builder worker process.

    Multiple-model semantics, idle-release scanner, fast-path for
    already-loaded models, and ``_mark_dead`` cleanup all match the
    legacy host (see SSOT §4 / §5).
    """

    HEALTH_INTERVAL_S: float = _DEFAULT_HEALTH_INTERVAL_S
    FORCE_KILL_S: float = _DEFAULT_FORCE_KILL_S
    WORKER_READY_TIMEOUT_S: float = _DEFAULT_WORKER_READY_TIMEOUT_S
    LOAD_MODEL_TIMEOUT_S: float = _DEFAULT_LOAD_MODEL_TIMEOUT_S
    PING_BUDGET_BASE_S: float = _DEFAULT_PING_BUDGET_BASE_S
    PING_BUDGET_PER_RELEASE_S: float = _DEFAULT_PING_BUDGET_PER_RELEASE_S

    __slots__ = (
        "_bootstrap",
        "_spawn_fn",
        "_proc",
        "_state",
        "_loaded_models",
        "_active_model_id",
        "_multimodel",
        "_idle_release_seconds",
        "_health_task",
        "_stderr_task",
        "_stderr_queue",
        "_recent_stderr",
        "_op_lock",
        "_idle_release_lock",
        "_last_used_at",
        "_kill_group",
        # Auto-respawn state
        "_event_bus",
        "_respawn_delay_s",
        "_max_respawn_attempts",
        "_respawn_window_s",
        "_respawn_history",
        "_respawn_task",
    )

    def __init__(
        self,
        *,
        bootstrap: BootstrapSpec,
        spawn_fn: SpawnFn | None = None,
        multimodel: bool | None = None,
        idle_release_seconds: int | None = None,
        event_bus: EventBus | None = None,
        respawn_delay_s: float = _DEFAULT_RESPAWN_DELAY_S,
        max_respawn_attempts: int = _DEFAULT_MAX_RESPAWN_ATTEMPTS,
        respawn_window_s: float = _DEFAULT_RESPAWN_WINDOW_S,
    ) -> None:
        self._bootstrap = bootstrap
        self._spawn_fn = spawn_fn or _default_spawn
        self._proc: asyncio.subprocess.Process | None = None
        self._state: str = "absent"
        self._loaded_models: dict[str, LoadedModelEntry] = {}
        self._active_model_id: str | None = None
        self._multimodel: bool = (
            multimodel if multimodel is not None else _multimodel_enabled_default()
        )
        self._idle_release_seconds: int = (
            idle_release_seconds
            if idle_release_seconds is not None
            else _idle_release_seconds_default()
        )
        self._health_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_queue: asyncio.Queue[str] = asyncio.Queue()
        # Rolling tail of the most recent worker stderr lines. Kept separate
        # from ``_stderr_queue`` (which is drained per-run for the UI log
        # stream) so that when the worker dies we can attach the last thing it
        # printed — usually the native traceback / QNN error — to the
        # ``_mark_dead`` diagnostic even if no run was streaming at the time.
        self._recent_stderr: deque[str] = deque(maxlen=50)
        # Serialises protocol ops (load / run / cancel / release / ping).
        self._op_lock = asyncio.Lock()
        self._idle_release_lock = asyncio.Lock()
        self._last_used_at: float = time.time()
        # State-Truth-First 铁律 5: OS-level "parent dies -> child dies"
        # safeguard. If the API process is force-killed (Task Manager /
        # power loss) before lifespan shutdown can run op:shutdown, the Job
        # Object reaps the worker subprocess (and its NPU-resident models) so
        # it never becomes an orphan that blocks the next boot's spawn /
        # holds the NPU. Shared cross-context util from the platform kernel;
        # graceful no-op on non-Windows. Created lazily on first spawn.
        self._kill_group: ProcessKillGroup | None = None
        # Auto-respawn configuration and state
        self._event_bus: EventBus | None = event_bus
        self._respawn_delay_s = respawn_delay_s
        self._max_respawn_attempts = max_respawn_attempts
        self._respawn_window_s = respawn_window_s
        # Rolling history of respawn timestamps for rate-limiting
        self._respawn_history: deque[float] = deque()
        self._respawn_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public read accessors (used by StickyWorkerStatusAdapter)
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def alive(self) -> bool:
        return self._state not in ("dead", "absent")

    @property
    def multimodel(self) -> bool:
        return self._multimodel

    @property
    def active_model_id(self) -> str | None:
        return self._active_model_id

    @property
    def idle_release_seconds(self) -> int:
        return self._idle_release_seconds

    def loaded_models_snapshot(self) -> tuple[LoadedModelEntry, ...]:
        """Return an immutable snapshot of the loaded-models registry.

        Snapshot semantics let consumers inspect the host without
        racing on the live dict; entries themselves are mutable
        dataclasses so we copy each one to keep the snapshot immutable
        for the duration of the read.
        """
        return tuple(
            LoadedModelEntry(
                model_id=e.model_id,
                variant_id=e.variant_id,
                last_used_at=e.last_used_at,
                state=e.state,
            )
            for e in self._loaded_models.values()
        )

    def is_loaded(self, model_id: str, variant_id: str | None = None) -> bool:
        """Fast path: ``True`` iff this exact (model_id, variant_id) is loaded."""
        entry = self._loaded_models.get(model_id)
        if entry is None:
            return False
        if variant_id is None:
            return True
        return entry.variant_id == variant_id

    # ------------------------------------------------------------------
    # Spawn — bootstrap subprocess + worker_ready handshake
    # ------------------------------------------------------------------
    async def spawn(self) -> None:
        """Spawn the bootstrap subprocess and wait for ``worker_ready``."""
        if self._proc is not None and self._state != "dead":
            return
        logger.info("StickyWorkerHost.spawn: argv=%s", self._bootstrap.argv)
        try:
            proc = await self._spawn_fn(self._bootstrap)
        except (OSError, FileNotFoundError) as exc:
            self._state = "dead"
            raise StickyWorkerSpawnError(
                f"Failed to spawn worker process: {exc}"
            ) from exc
        self._proc = proc
        self._state = "idle"
        self._last_used_at = time.time()

        # State-Truth-First 铁律 5: assign the freshly-spawned worker to the
        # OS kill-group so a force-killed API parent cannot orphan it (Job
        # Object KILL_ON_JOB_CLOSE on Windows; no-op elsewhere). Lazily
        # created + best-effort: a failure here never blocks the spawn (the
        # graceful op:shutdown path remains the primary teardown).
        if self._kill_group is None:
            self._kill_group = ProcessKillGroup()
        pid = getattr(proc, "pid", None)
        if pid is not None:
            self._kill_group.assign(int(pid))

        try:
            await self._await_status(
                "worker_ready", timeout=self.WORKER_READY_TIMEOUT_S
            )
        except (asyncio.TimeoutError, _WorkerDead) as exc:
            await self._force_kill()
            raise StickyWorkerSpawnError(
                f"Worker process did not signal worker_ready within "
                f"{self.WORKER_READY_TIMEOUT_S}s: {exc}"
            ) from exc

        logger.info("StickyWorkerHost spawned pid=%s", self._proc.pid)
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="sticky_worker.stderr"
        )
        self._health_task = asyncio.create_task(
            self._health_check_loop(), name="sticky_worker.health"
        )

    # ------------------------------------------------------------------
    # load_model
    # ------------------------------------------------------------------
    async def load_model(self, request: LoadModelRequest) -> bool:
        """Load (or fast-path-confirm) ``request`` into the worker.

        Returns ``True`` when an actual load happened, ``False`` on
        fast-path (model already loaded with the same variant).
        """
        if self._state == "dead":
            raise RuntimeError("Cannot load model: worker is dead")
        if self._proc is None:
            raise RuntimeError("Cannot load model: worker not spawned")

        # Fast path: same (model_id, variant_id) already loaded.
        existing = self._loaded_models.get(request.model_id)
        if existing is not None and existing.variant_id == request.variant_id:
            existing.touch()
            self._active_model_id = request.model_id
            self._last_used_at = existing.last_used_at
            if self._state == "idle":
                self._state = "ready"
            logger.debug(
                "StickyWorkerHost fast-path load %s/%s (cached)",
                request.model_id, request.variant_id,
            )
            return False

        async with self._op_lock:
            # In single-model mode, drop any other entries — bootstrap
            # also drops its side per QAI_STICKY_MULTIMODEL.
            if not self._multimodel:
                self._loaded_models.clear()

            self._state = "loading"
            await self._suspend_health_task()
            try:
                self._write_op(request.to_op_payload())
                await self._proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self._mark_dead(reason=f"op:load stdin write failed: {exc!r}")
                raise RuntimeError(
                    f"Failed to send op:load: {exc}"
                ) from exc

            try:
                await self._await_status(
                    "model_loaded", timeout=self.LOAD_MODEL_TIMEOUT_S
                )
            except _WorkerDead as exc:
                self._mark_dead(reason=f"worker exited during op:load: {exc}")
                raise RuntimeError(f"Worker died during op:load: {exc}") from exc
            except asyncio.TimeoutError as exc:
                # Don't kill the process — it may still be usable for a
                # different model. Roll back local state.
                if self._proc.returncode is not None:
                    self._mark_dead(
                        reason="op:load timed out AND worker already exited"
                    )
                else:
                    self._state = (
                        "ready" if self._loaded_models else "idle"
                    )
                self._restart_health_task_after_op()
                raise RuntimeError(
                    f"Worker failed to load model within "
                    f"{self.LOAD_MODEL_TIMEOUT_S}s"
                ) from exc

            now = time.time()
            self._loaded_models[request.model_id] = LoadedModelEntry(
                model_id=request.model_id,
                variant_id=request.variant_id,
                last_used_at=now,
                state="ready",
            )
            self._active_model_id = request.model_id
            self._state = "ready"
            self._last_used_at = now
            self._restart_health_task_after_op()
            logger.debug(
                "StickyWorkerHost loaded %s/%s",
                request.model_id, request.variant_id,
            )
            return True

    # ------------------------------------------------------------------
    # execute_run
    # ------------------------------------------------------------------
    async def execute_run(
        self, request: RunRequest
    ) -> AsyncIterator[WorkerEvent]:
        """Send op:run and yield :class:`WorkerEvent` envelopes.

        Termination conditions:

        * a ``done`` or ``error`` frame from the bootstrap;
        * worker process exits unexpectedly (raises :class:`RuntimeError`);
        * stdout read times out (raises :class:`RuntimeError`).
        """
        if self._state != "ready":
            raise RuntimeError(
                f"Worker not ready for run (state={self._state})"
            )
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("Worker not spawned")

        # Don't take _op_lock for the whole run (may stream for minutes);
        # instead transition the host state to busy and let other ops
        # serialise on _op_lock at their own boundary.
        self._state = "busy"
        info = self._loaded_models.get(request.model_id)
        if info is not None:
            info.state = "busy"
        await self._suspend_health_task()

        # Drain residual stderr before the run starts so the per-run
        # log stream is clean (legacy parity).
        while True:
            try:
                self._stderr_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        try:
            logger.info("StickyWorkerHost.execute_run: sending op:run to worker run_id=%s model=%s", request.run_id, self._active_model_id)
            self._write_op(request.to_op_payload())
            await self._proc.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._mark_dead(reason=f"op:run stdin write failed: {exc!r}")
            raise RuntimeError(f"Failed to send op:run: {exc}") from exc

        try:
            async for ev in self._stream_run_events(request.run_id):
                yield ev
        finally:
            # Restore state regardless of how the iteration ended.
            now = time.time()
            self._last_used_at = now
            info = self._loaded_models.get(request.model_id)
            if info is not None:
                info.last_used_at = now
                info.state = "ready"
            if self._state == "busy":
                self._state = "ready"
            self._restart_health_task_after_op()

    async def _stream_run_events(
        self, run_id: str
    ) -> AsyncIterator[WorkerEvent]:
        assert self._proc is not None and self._proc.stdout is not None
        logger.info("StickyWorkerHost.execute_run: waiting for worker stdout run_id=%s", run_id)
        while True:
            for log_ev in self._drain_pending_stderr(run_id):
                yield log_ev
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=300.0
                )
            except asyncio.TimeoutError as exc:
                for log_ev in self._drain_pending_stderr(run_id):
                    yield log_ev
                self._mark_dead(
                    reason=f"stdout read timed out (300s) during run {run_id}"
                )
                raise RuntimeError(
                    "Timeout waiting for worker output"
                ) from exc
            if not line:
                # EOF
                for log_ev in self._drain_pending_stderr(run_id):
                    yield log_ev
                self._mark_dead(
                    reason=(
                        f"stdout EOF during run {run_id} — worker process "
                        "exited/crashed mid-run"
                    )
                )
                raise RuntimeError(
                    "Worker process exited unexpectedly during run"
                )
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            frame = BootstrapProtocol.decode_line(text)
            payload: dict[str, Any] = dict(frame.payload)
            payload.setdefault("runId", run_id)
            kind = frame.kind
            if kind == "unknown":
                # Surface as a log line so the UI can show it.
                yield WorkerEvent(
                    type="log",
                    payload={
                        "stream": "stdout",
                        "line": frame.raw,
                        "runId": run_id,
                    },
                )
                continue
            yield WorkerEvent(type=kind, payload=payload)
            if kind in ("done", "error"):
                for log_ev in self._drain_pending_stderr(run_id):
                    yield log_ev
                return

    def _drain_pending_stderr(self, run_id: str) -> list[WorkerEvent]:
        events: list[WorkerEvent] = []
        while True:
            try:
                text = self._stderr_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            events.append(
                WorkerEvent(
                    type="log",
                    payload={
                        "stream": "stderr",
                        "line": text,
                        "runId": run_id,
                    },
                )
            )
        return events

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------
    async def cancel_run(self, run_id: str) -> None:
        """Send op:cancel for the given run id (best-effort)."""
        if self._state == "dead" or self._proc is None:
            return
        if self._proc.stdin is None:
            return
        try:
            self._write_op({"op": "cancel", "runId": run_id})
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._mark_dead(reason=f"op:cancel stdin write failed: {exc!r}")

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------
    async def release_model(self, model_id: str) -> None:
        """Send op:release for ``model_id`` (idempotent).

        The ``op:release`` write is serialised on ``_op_lock`` — the same
        lock guarding ``load`` / ``run`` / ``ping`` — so the idle-release
        scanner can never interleave a release frame onto the shared
        stdin/stdout pipe with a concurrent op. Without this, an
        ``execute_run`` (or ``ping``) op racing the idle scanner produces
        interleaved protocol frames on the pipe and can tear down the
        worker's native model mid-use, surfacing as a hard subprocess
        crash (0xFFFFFFFF). ``_idle_release_lock`` still guards concurrent
        idle-release scans; it is acquired *outside* ``_op_lock`` and both
        are released before the caller (``_health_check_loop``) issues its
        follow-up ``ping`` (which acquires ``_op_lock`` on its own), so no
        cross-op lock is ever held and there is no deadlock.
        """
        if self._state == "dead" or self._proc is None:
            return
        async with self._idle_release_lock, self._op_lock:
            if self._state == "dead" or self._proc is None:
                return
            # Re-validate under the lock. The idle scanner snapshots the
            # release candidates *before* acquiring _op_lock, so a run may
            # have arrived in that window and marked this model busy (or
            # flipped the host to busy). Releasing it now would tear down a
            # native model that is mid-use — exactly the class of race that
            # crashes the worker. If it is no longer an idle "ready" entry,
            # skip the op entirely (no frame is written to the pipe).
            entry = self._loaded_models.get(model_id)
            if entry is None or entry.state != "ready" or self._state == "busy":
                logger.info(
                    "StickyWorkerHost idle-release SKIPPED %s under lock "
                    "(entry=%s host_state=%s) — a run raced the scanner; "
                    "not tearing down a model that is (or just became) in "
                    "use. This is the guard against the mid-use native "
                    "teardown crash.",
                    model_id,
                    entry.state if entry is not None else "<gone>",
                    self._state,
                )
                return
            try:
                self._write_op({"op": "release", "modelId": model_id})
                await self._proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                logger.warning(
                    "release op IPC failed for %s: %s", model_id, exc
                )
                return
            info = self._loaded_models.pop(model_id, None)
            if info is not None:
                logger.info(
                    "StickyWorkerHost idle-released %s (idle %.1fs)",
                    model_id, time.time() - info.last_used_at,
                )
            if self._active_model_id == model_id:
                self._active_model_id = None
                if not self._loaded_models:
                    self._state = "idle"

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------
    async def shutdown(self, *, reason: str = "host_shutdown") -> None:
        """Gracefully shut the worker down.

        Sends op:shutdown, waits up to :attr:`FORCE_KILL_S` for graceful
        exit, otherwise SIGKILLs. Always ends in ``state="dead"``.
        """
        if self._state == "dead":
            return
        if self._proc is None:
            self._state = "absent"
            return
        logger.info(
            "StickyWorkerHost.shutdown reason=%s pid=%s",
            reason, self._proc.pid,
        )
        self._state = "shutting_down"
        # Cancel background tasks first so they don't read stdout
        # concurrently with the shutdown handshake.
        await self._cancel_task("_health_task")
        await self._cancel_task("_stderr_task")
        await self._cancel_task("_respawn_task")

        try:
            self._write_op({"op": "shutdown"})
            assert self._proc.stdin is not None
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self.FORCE_KILL_S)
        except asyncio.TimeoutError:
            logger.warning(
                "worker did not exit within %.1fs, killing pid=%s",
                self.FORCE_KILL_S, self._proc.pid,
            )
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        self._mark_dead(reason=f"graceful shutdown ({reason})")

    # ------------------------------------------------------------------
    # ping (synchronous health check)
    # ------------------------------------------------------------------
    async def ping(self, *, timeout_s: float | None = None) -> bool:
        """Send op:ping and wait for pong. Returns True iff pong arrived."""
        if self._state == "dead" or self._proc is None:
            return False
        if self._proc.stdin is None or self._proc.stdout is None:
            return False
        budget = (
            timeout_s if timeout_s is not None else self.PING_BUDGET_BASE_S
        )
        async with self._op_lock:
            try:
                self._write_op({"op": "ping"})
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self._mark_dead(reason=f"op:ping stdin write failed: {exc!r}")
                return False
            deadline = time.time() + budget
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    line = await asyncio.wait_for(
                        self._proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    return False
                if not line:
                    self._mark_dead(reason="stdout EOF while awaiting pong")
                    return False
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                frame = BootstrapProtocol.decode_line(text)
                if frame.kind == "pong":
                    return True
                # Tolerate model_released frames that arrive in this window.
                if (
                    frame.kind == "status"
                    and frame.state == "model_released"
                ):
                    continue
            return False

    # ------------------------------------------------------------------
    # Health-check loop
    # ------------------------------------------------------------------
    async def _health_check_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.HEALTH_INTERVAL_S)
                if self._proc is None or self._proc.returncode is not None:
                    self._mark_dead(
                        reason="health loop observed process already exited"
                    )
                    return
                if self._state == "busy":
                    continue
                released = 0
                if self._multimodel and self._loaded_models:
                    threshold = self._idle_release_seconds
                    now = time.time()
                    to_release = [
                        mid
                        for mid, info in self._loaded_models.items()
                        if info.state == "ready"
                        and (now - info.last_used_at) >= threshold
                    ]
                    if to_release:
                        logger.info(
                            "StickyWorkerHost idle-release scan: releasing %s "
                            "(threshold=%ss, active_model=%s, state=%s)",
                            to_release,
                            threshold,
                            self._active_model_id,
                            self._state,
                        )
                    for mid in to_release:
                        await self.release_model(mid)
                        released += 1
                budget = (
                    self.PING_BUDGET_BASE_S
                    + self.PING_BUDGET_PER_RELEASE_S * released
                )
                ok = await self.ping(timeout_s=budget)
                if not ok:
                    self._mark_dead(
                        reason=(
                            f"health-check ping failed "
                            f"(released={released} model(s) this scan, "
                            f"budget={budget:.1f}s)"
                        )
                    )
                    return
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Stderr drain
    # ------------------------------------------------------------------
    async def _drain_stderr(self) -> None:
        try:
            assert self._proc is not None
            assert self._proc.stderr is not None
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Surface worker diagnostic lines (perf-diag, *-diag, bootstrap)
                # into the host log file so they are visible without needing the
                # browser console. These markers are emitted by runner scripts.
                if "-diag]" in text or "[bootstrap]" in text or "[perf-diag]" in text:
                    logger.info("worker.diag %s", text)
                # Keep a rolling tail for crash diagnostics (see _mark_dead).
                self._recent_stderr.append(text)
                with contextlib.suppress(asyncio.QueueFull):
                    self._stderr_queue.put_nowait(text)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _write_op(self, payload: Mapping[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = BootstrapProtocol.encode_op(payload)
        self._proc.stdin.write(data)

    async def _await_status(self, expected_state: str, *, timeout: float) -> None:
        """Read frames until a ``{type:status, state:expected_state}`` arrives."""
        assert self._proc is not None and self._proc.stdout is not None
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for status '{expected_state}'"
                )
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=remaining
                )
            except asyncio.TimeoutError as exc:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for status '{expected_state}'"
                ) from exc
            if not line:
                raise _WorkerDead(
                    f"worker exited before emitting '{expected_state}'"
                )
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            frame = BootstrapProtocol.decode_line(text)
            if frame.kind == "status" and frame.state == expected_state:
                return
            if frame.kind == "error":
                code = frame.payload.get("code", "UNKNOWN")
                message = frame.payload.get("message", "")
                raise _WorkerDead(
                    f"worker emitted error during startup: {code}: {message}"
                )
            # Otherwise: ignore (status events for unrelated states).

    async def _suspend_health_task(self) -> None:
        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._health_task
            self._health_task = None

    def _restart_health_task_after_op(self) -> None:
        if self._state in ("dead", "shutting_down"):
            return
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(
            self._health_check_loop(), name="sticky_worker.health"
        )

    async def _cancel_task(self, attr: str) -> None:
        task: asyncio.Task[None] | None = getattr(self, attr)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        setattr(self, attr, None)

    def _mark_dead(self, reason: str = "unspecified") -> None:
        """Centralised "worker died" cleanup.

        Drops the loaded-models registry so peers reading the snapshot
        don't see entries pointing at a zombie process.

        Every path that concludes the worker is gone funnels through here,
        so this is the single best place to emit a self-diagnosing record:
        the caller-supplied ``reason``, the OS ``returncode`` (decoded for
        Windows NTSTATUS / POSIX signals), which model was active, and the
        last few lines the worker printed to stderr (usually the native
        traceback / QNN teardown error that explains a 0xFFFFFFFF crash).
        Idempotent: if we were already ``dead`` we skip the (noisy) log.

        After logging, schedules an automatic respawn attempt (if the
        respawn budget has not been exhausted) so subsequent runs can
        resume using the persistent worker instead of falling back to
        one-shot indefinitely.
        """
        already_dead = self._state == "dead"
        self._state = "dead"
        active = self._active_model_id
        loaded = list(self._loaded_models.keys())
        self._loaded_models.clear()
        self._active_model_id = None
        if already_dead:
            return
        rc = self._proc.returncode if self._proc is not None else None
        pid = self._proc.pid if self._proc is not None else None
        tail = list(self._recent_stderr)[-10:]
        logger.warning(
            "StickyWorkerHost worker DEAD reason=%s pid=%s %s active_model=%s "
            "loaded=%s stderr_tail=%s",
            reason,
            pid,
            _describe_returncode(rc),
            active,
            loaded,
            tail if tail else "<none captured>",
        )
        logger.info(
            "StickyWorkerHost._mark_dead: active_model=%s notifying broadcaster of failure (reason=%s)",
            active,
            reason,
        )
        # Determine whether we can/will respawn (for the event payload).
        will_respawn = self._can_respawn(reason)
        # Publish death event via the shared EventBus (best-effort, never
        # blocks the death path).
        self._publish_death_event(reason=reason, pid=pid, will_respawn=will_respawn)
        # Schedule auto-respawn if budget allows.
        if will_respawn:
            self._schedule_respawn()

    def _can_respawn(self, reason: str) -> bool:
        """Return True if an auto-respawn should be attempted.

        Respawn is suppressed for graceful shutdowns and when the
        rolling attempt budget is exhausted.
        """
        # Never respawn after an intentional shutdown.
        if "graceful shutdown" in reason or "host_shutdown" in reason:
            return False
        # Prune old entries outside the rolling window.
        cutoff = time.time() - self._respawn_window_s
        while self._respawn_history and self._respawn_history[0] < cutoff:
            self._respawn_history.popleft()
        if len(self._respawn_history) >= self._max_respawn_attempts:
            logger.warning(
                "StickyWorkerHost auto-respawn SUPPRESSED: %d attempts in "
                "last %.0fs (max %d)",
                len(self._respawn_history),
                self._respawn_window_s,
                self._max_respawn_attempts,
            )
            return False
        return True

    def _schedule_respawn(self) -> None:
        """Fire-and-forget an async respawn task after a short delay."""
        # Cancel any already-pending respawn task to avoid double-spawns.
        if self._respawn_task is not None and not self._respawn_task.done():
            self._respawn_task.cancel()
        self._respawn_task = asyncio.create_task(
            self._do_respawn(), name="sticky_worker.respawn"
        )

    async def _do_respawn(self) -> None:
        """Kill the old process (if lingering) and spawn a fresh worker.

        Runs as a fire-and-forget task so the synchronous ``_mark_dead``
        caller is never blocked. The existing ``spawn()`` method is
        reused for all process creation / handshake logic.
        """
        attempt = len(self._respawn_history) + 1
        logger.info(
            "StickyWorkerHost auto-respawn: waiting %.1fs before attempt %d/%d",
            self._respawn_delay_s,
            attempt,
            self._max_respawn_attempts,
        )
        await asyncio.sleep(self._respawn_delay_s)

        # Record this attempt in the rolling history.
        self._respawn_history.append(time.time())

        # Kill the old process if it's still lingering (returncode is None).
        old_proc = self._proc
        if old_proc is not None and old_proc.returncode is None:
            logger.info(
                "StickyWorkerHost respawn: killing lingering old worker pid=%s",
                old_proc.pid,
            )
            with contextlib.suppress(ProcessLookupError, OSError):
                old_proc.kill()
            with contextlib.suppress(Exception):
                await old_proc.wait()

        # Reset internal state so ``spawn()`` proceeds cleanly.
        self._proc = None
        self._state = "absent"
        self._recent_stderr.clear()
        # Drain any stale stderr items from the old process.
        while not self._stderr_queue.empty():
            try:
                self._stderr_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        try:
            await self.spawn()
        except (StickyWorkerSpawnError, Exception) as exc:
            logger.error(
                "StickyWorkerHost auto-respawn FAILED (attempt %d/%d): %s",
                attempt,
                self._max_respawn_attempts,
                exc,
            )
            # State remains dead; a subsequent _mark_dead call from
            # spawn internals will evaluate the budget again.
            return

        new_pid = self._proc.pid if self._proc is not None else None
        logger.info(
            "StickyWorkerHost auto-respawn SUCCEEDED (attempt %d/%d) "
            "new_pid=%s state=%s",
            attempt,
            self._max_respawn_attempts,
            new_pid,
            self._state,
        )
        # Publish recovery event.
        self._publish_recovery_event(pid=new_pid, attempt=attempt)

    # ------------------------------------------------------------------
    # Event publishing helpers (best-effort, never raise)
    # ------------------------------------------------------------------
    def _publish_death_event(
        self,
        *,
        reason: str,
        pid: int | None,
        will_respawn: bool,
    ) -> None:
        """Publish a StickyWorkerDiedEvent if an EventBus is wired."""
        if self._event_bus is None:
            return
        try:
            from qai.app_builder.domain.events import StickyWorkerDiedEvent

            event = StickyWorkerDiedEvent(
                reason=reason, pid=pid, will_respawn=will_respawn
            )
            asyncio.create_task(
                self._event_bus.publish(event),
                name="sticky_worker.publish_died",
            )
        except Exception:  # noqa: BLE001 — event publish must never disrupt
            logger.debug("Failed to publish StickyWorkerDiedEvent", exc_info=True)

    def _publish_recovery_event(
        self, *, pid: int | None, attempt: int
    ) -> None:
        """Publish a StickyWorkerRecoveredEvent if an EventBus is wired."""
        if self._event_bus is None:
            return
        try:
            from qai.app_builder.domain.events import StickyWorkerRecoveredEvent

            event = StickyWorkerRecoveredEvent(pid=pid, respawn_attempt=attempt)
            asyncio.create_task(
                self._event_bus.publish(event),
                name="sticky_worker.publish_recovered",
            )
        except Exception:  # noqa: BLE001 — event publish must never disrupt
            logger.debug(
                "Failed to publish StickyWorkerRecoveredEvent", exc_info=True
            )

    async def _force_kill(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        self._mark_dead(reason="force-killed (spawn handshake failed)")
