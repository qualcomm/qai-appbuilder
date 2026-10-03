# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Transactional QAIRT SDK switch service (Task 5).

:class:`QairtSwitchService` is the single orchestrator for a QAIRT SDK
hot-switch: it owns job lifecycle (create/poll/retain), the switch-only
reservation that guarantees at most one switch runs at a time, and the
full transaction that ensures the extension, activates the new config,
recycles the sticky worker, and rolls back cleanly on any failure.

Layering (Locked Contracts, ``docs/superpowers/plans/
2026-09-06-qairt-sdk-hot-switch.md``): this module lives under
``qai.platform.qairt_switch`` and must NEVER import ``qai.app_builder.*``
— the sticky-worker stop/restart adapter and the extension installer are
both received as constructor-injected ports (``StickyWorkerLifecyclePort``
/ the already-independent ``CompatibleExtensionInstaller``), never
imported directly from a bounded context.

Transaction order (Task 5 pseudocode)::

    reserve active switch slot before returning to the caller
    -> validate installed target and explicit no-QNN-inference acknowledgement
    -> snapshot active JSON
    -> remember whether sticky worker is running
    -> stop sticky worker if running, before any wheel replacement
    -> try:
         install and verify extension if required
           (restoring the original extension before any failed-install outcome
            -- this step is the installer's own responsibility, see
            CompatibleExtensionInstaller._recover_or_raise)
         -> atomically activate target JSON
         -> restart sticky worker if it was running
         -> fresh child runtime probe using the injected probe port
         -> publish succeeded
       except BaseException:
         create and await a shielded recovery task:
           stop partial new worker
           -> atomically restore exact old JSON if configuration was committed
           -> recreate old worker if it was previously running
         publish rolled_back only if required config/worker recovery succeeds
         otherwise publish rollback_failed with recovery instructions
         re-raise cancellation after protected recovery completes
       finally:
         release the switch-only reservation exactly once
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from qai.platform.qairt_switch.config_repository import QairtConfigPathError
from qai.platform.qairt_switch.models import (
    CandidateAttempt,
    DownloadProgress,
    Phase,
    SwitchDiagnostics,
)
from qai.platform.qairt_switch.versioning import AppBuilderVersion, QairtVersion

if TYPE_CHECKING:
    from pathlib import Path

    from qai.platform.qairt_switch.config_repository import (
        QairtConfigRepository,
        QairtConfigSnapshot,
    )
    from qai.platform.qairt_switch.installer import CompatibleExtensionInstaller

__all__ = [
    "AlreadyRunningError",
    "InvalidTargetError",
    "MissingAcknowledgementError",
    "QairtSwitchService",
    "RuntimeProbePort",
    "StickyWorkerLifecyclePort",
    "SwitchJob",
]

#: Fixed, unqueried Issue-reporting link surfaced in every job's diagnostics.
_ISSUE_URL = "https://github.com/qualcomm/qai-appbuilder/issues/new"

#: In-memory job retention (Task 5): only the active job plus the last 20
#: terminal jobs are kept, and terminal entries older than this age are
#: evicted on the next ``create_job``/``get_job`` call. No DB migration.
_MAX_TERMINAL_JOBS = 20
_TERMINAL_JOB_MAX_AGE_S = 24 * 60 * 60


class StickyWorkerLifecyclePort(Protocol):
    """The exact subset of ``StickyWorkerLifecycle`` this service calls.

    A ``Protocol`` (not an import of the concrete App Builder type) so
    ``qai.platform.qairt_switch`` never depends on ``qai.app_builder.*``
    — the composition root (``apps.api.di``) injects the real
    ``qai.app_builder.infrastructure.sticky_worker.StickyWorkerLifecycle``
    instance, which already satisfies this shape structurally.
    """

    @property
    def running(self) -> bool: ...

    async def stop_for_qairt_switch(self) -> bool: ...

    async def start_after_qairt_switch(self) -> None: ...


class RuntimeProbePort(Protocol):
    """Fresh-child runtime verification after activation.

    Injected so the service never constructs a command resolver /
    subprocess itself (that machinery is App Builder infrastructure);
    the composition root wires a callable that spawns one throwaway
    child through the production command resolver and confirms it
    observes the newly-activated ``QAIRT_ROOT``.
    """

    async def __call__(self, *, expected_root: Path) -> None: ...


class RuntimeProbeVerificationError(RuntimeError):
    """Raised by a :class:`RuntimeProbePort` implementation when the fresh
    child did not observe the newly-activated ``QAIRT_ROOT``.

    ``error_kind``/``error_hint`` are allowlisted (path-free) so
    :func:`_classify_switch_failure` never falls through to ``str(exc)``
    for this failure — the constructor message MAY carry the observed/
    expected filesystem paths for server-side logging, but that message
    is never itself surfaced to the client.
    """

    error_kind = "runtime_probe_failed"
    error_hint = "The new QAIRT SDK could not be verified after activation."


class AlreadyRunningError(RuntimeError):
    """Raised by :meth:`QairtSwitchService.create_job` when a switch is already in flight."""


class MissingAcknowledgementError(ValueError):
    """Raised when ``no_qnn_inference_acknowledged`` is not explicitly ``True``."""


class InvalidTargetError(ValueError):
    """Raised when the requested target version is not an installed QAIRT SDK."""


@dataclass(slots=True)
class SwitchJob:
    """Mutable job record. Only :class:`QairtSwitchService` mutates instances."""

    job_id: str
    diagnostics: SwitchDiagnostics
    created_at: float

    @property
    def is_terminal(self) -> bool:
        return self.diagnostics.is_terminal


def _new_diagnostics(
    *, target_version: QairtVersion, active_version: QairtVersion | None, architecture: str
) -> SwitchDiagnostics:
    return SwitchDiagnostics(
        phase=Phase.VALIDATING_TARGET,
        target_version=target_version,
        active_version=active_version,
        architecture=architecture,
        correlation_id=uuid.uuid4().hex,
        issue_url=_ISSUE_URL,
    )


class QairtSwitchService:
    """Orchestrates one QAIRT SDK hot-switch job at a time.

    Construct exactly once at the composition root with every
    collaborator already wired: the config repository bound to the
    live ``qairt_env.json``, the compatible-extension installer, the
    sticky-worker lifecycle port, and a fresh-child runtime probe.
    """

    __slots__ = (
        "_active_job_id",
        "_architecture",
        "_config_repository",
        "_install_root",
        "_installer",
        "_jobs",
        "_runtime_probe",
        "_sticky_worker",
    )

    def __init__(
        self,
        *,
        config_repository: QairtConfigRepository,
        installer: CompatibleExtensionInstaller,
        sticky_worker: StickyWorkerLifecyclePort,
        runtime_probe: RuntimeProbePort,
        install_root: Path,
        architecture: str,
    ) -> None:
        self._config_repository = config_repository
        self._installer = installer
        self._sticky_worker = sticky_worker
        self._runtime_probe = runtime_probe
        self._install_root = install_root
        self._architecture = architecture
        self._jobs: OrderedDict[str, SwitchJob] = OrderedDict()
        self._active_job_id: str | None = None

    # ── read accessors for the preflight route (no mutation) ────────────

    @property
    def sticky_worker_running(self) -> bool:
        """Whether the sticky worker is currently running (informational)."""
        return self._sticky_worker.running

    async def inspect_installed_extension_version(self) -> AppBuilderVersion | None:
        """Return the currently-installed ``qai_appbuilder`` version, or ``None``.

        Delegates to the injected installer's own fresh-subprocess
        inspection; never imports the extension in-process.
        """
        return await self._installer.inspect_installed_version()

    # ── job lookup (read-only, side-effect free) ────────────────────────

    def get_job(self, job_id: str) -> SwitchJob | None:
        self._evict_stale_terminal_jobs()
        return self._jobs.get(job_id)

    # ── job creation: the atomic reservation ────────────────────────────

    def create_job(
        self,
        *,
        target_version: str,
        no_qnn_inference_acknowledged: bool,
        active_version: str | None,
    ) -> SwitchJob:
        """Reserve the single active-switch slot and register a new job.

        Every check here is synchronous — there is no ``await`` between
        "is a switch already running" and "mark one as running" — so two
        concurrent calls can never both observe an empty slot (the
        precise TOCTOU window Contract 6/Task 5 forbids). The caller
        (the HTTP route, Task 6) is responsible for actually running the
        transaction via :meth:`run_switch` after this returns.
        """
        self._evict_stale_terminal_jobs()
        if self._active_job_id is not None:
            raise AlreadyRunningError(
                "A QAIRT SDK switch is already in progress; "
                "wait for it to finish before starting another."
            )
        if no_qnn_inference_acknowledged is not True:
            raise MissingAcknowledgementError(
                "no_qnn_inference_acknowledged must be explicitly true: the "
                "user must confirm no QNN model inference task is running "
                "in the current application before a switch can begin."
            )
        try:
            target = QairtVersion.parse(target_version)
            self._config_repository.resolve_target(self._install_root, target)
        except (ValueError, QairtConfigPathError) as exc:
            raise InvalidTargetError(
                f"Not an installed QAIRT version: {target_version!r}"
            ) from exc

        active: QairtVersion | None = None
        if active_version is not None:
            try:
                active = QairtVersion.parse(active_version)
            except ValueError:
                active = None

        job_id = uuid.uuid4().hex
        job = SwitchJob(
            job_id=job_id,
            diagnostics=_new_diagnostics(
                target_version=target, active_version=active, architecture=self._architecture
            ),
            created_at=time.time(),
        )
        self._jobs[job_id] = job
        self._active_job_id = job_id
        return job

    # ── the transaction ──────────────────────────────────────────────────

    async def run_switch(self, job_id: str) -> None:
        """Run the full switch transaction for a job created by :meth:`create_job`.

        Always releases the active-switch reservation exactly once,
        regardless of success, failure, or cancellation (``finally``).
        Never raises to the caller — every failure is recorded onto the
        job's terminal diagnostics instead, so the HTTP layer (Task 6)
        only ever needs to poll :meth:`get_job`.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        try:
            await self._run_switch_locked(job)
        finally:
            if self._active_job_id == job_id:
                self._active_job_id = None
            self._evict_stale_terminal_jobs()

    async def _run_switch_locked(self, job: SwitchJob) -> None:
        target = job.diagnostics.target_version
        snapshot: QairtConfigSnapshot | None = None
        config_committed = False
        was_running = False

        try:
            snapshot = self._config_repository.snapshot()
            was_running = self._sticky_worker.running
            if was_running:
                self._set_phase(job, Phase.STOPPING_WORKER)
                await self._sticky_worker.stop_for_qairt_switch()

            attempts: tuple[CandidateAttempt, ...] = ()
            self._set_phase(job, Phase.CHECKING_EXTENSION)

            def _on_attempt(attempt: CandidateAttempt) -> None:
                self._set_diagnostics(
                    job, replace(job.diagnostics, attempts=(*job.diagnostics.attempts, attempt))
                )

            def _on_progress(progress: DownloadProgress) -> None:
                self._set_diagnostics(job, replace(job.diagnostics, progress=progress))

            result = await self._installer.ensure_compatible(
                target,
                on_phase=lambda phase: self._set_phase(job, phase),
                on_attempt=_on_attempt,
                on_progress=_on_progress,
            )
            attempts = result.attempts
            self._set_diagnostics(job, replace(job.diagnostics, attempts=attempts))

            self._set_phase(job, Phase.ACTIVATING)
            self._config_repository.activate(self._install_root, target)
            config_committed = True

            if was_running:
                self._set_phase(job, Phase.STARTING_WORKER)
                await self._sticky_worker.start_after_qairt_switch()

            self._set_phase(job, Phase.VERIFYING_RUNTIME)
            activated_root = self._install_root / str(target)
            await self._runtime_probe(expected_root=activated_root)

            self._set_diagnostics(
                job,
                replace(
                    job.diagnostics,
                    phase=Phase.COMPLETE,
                    active_version=target,
                ),
            )
        except BaseException as exc:

            error_code, error_message = _classify_switch_failure(exc)
            recovered = await asyncio.shield(
                self._recover(
                    job,
                    snapshot=snapshot,
                    config_committed=config_committed,
                    was_running=was_running,
                )
            )
            self._set_diagnostics(
                job,
                replace(
                    job.diagnostics,
                    phase=Phase.ROLLED_BACK if recovered else Phase.ROLLBACK_FAILED,
                    error_code=error_code,
                    error_message=error_message,
                ),
            )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _recover(
        self,
        job: SwitchJob,
        *,
        snapshot: QairtConfigSnapshot | None,
        config_committed: bool,
        was_running: bool,
    ) -> bool:
        """Restore the pre-switch config/worker state. Returns ``True`` iff fully recovered."""
        config_restored = True
        if config_committed and snapshot is not None:
            try:
                self._config_repository.restore(snapshot)
            except Exception:  # noqa: BLE001 — recovery must not raise
                config_restored = False

        worker_restored = True
        if was_running:
            try:
                await self._sticky_worker.stop_for_qairt_switch()
                await self._sticky_worker.start_after_qairt_switch()
            except Exception:  # noqa: BLE001 — recovery must not raise
                worker_restored = False

        return config_restored and worker_restored

    # ── job bookkeeping ──────────────────────────────────────────────────

    def _set_phase(self, job: SwitchJob, phase: Phase) -> None:
        self._set_diagnostics(job, replace(job.diagnostics, phase=phase))

    def _set_diagnostics(self, job: SwitchJob, diagnostics: SwitchDiagnostics) -> None:
        job.diagnostics = diagnostics

    def _evict_stale_terminal_jobs(self) -> None:
        """Keep only the active job plus the last 20 terminal jobs (<=24h old)."""
        now = time.time()
        terminal_ids = [
            jid
            for jid, job in self._jobs.items()
            if job.is_terminal and jid != self._active_job_id
        ]
        for jid in terminal_ids:
            job = self._jobs[jid]
            if now - job.created_at > _TERMINAL_JOB_MAX_AGE_S:
                del self._jobs[jid]
        terminal_ids = [
            jid
            for jid, job in self._jobs.items()
            if job.is_terminal and jid != self._active_job_id
        ]
        excess = len(terminal_ids) - _MAX_TERMINAL_JOBS
        for jid in terminal_ids[:max(excess, 0)]:
            del self._jobs[jid]


def _classify_switch_failure(exc: BaseException) -> tuple[str, str]:
    """Allowlisted ``(error_code, error_message)`` for a switch-transaction failure.

    Every exception type reachable from the transaction (extension
    install, config I/O, worker spawn, runtime-probe verification) sets
    class- or instance-level ``error_kind``/``error_hint`` specifically
    so this function never has to fall back to the raw exception text —
    ``str(exc)`` on any of those types may embed a local filesystem path
    or subprocess argv. The final ``return`` below is a last-resort for
    a genuinely unanticipated exception type; it deliberately reports
    only the class name, never ``str(exc)``, so an exception nobody
    thought to classify still cannot leak local server state to a
    client.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", "The switch was cancelled."
    error_kind = getattr(exc, "error_kind", None)
    error_hint = getattr(exc, "error_hint", None)
    if isinstance(error_kind, str) and isinstance(error_hint, str):
        return error_kind, error_hint
    return "switch_failed", f"An unexpected {exc.__class__.__name__} occurred."
