# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``DownloadModelWeightsUseCase`` — download a built-in Pack's weights.

Drives the shared multi-threaded aria2c download engine
(:class:`qai.platform.download.DownloadEnginePort`) to fetch a built-in
App Builder model's weight archive, streams live progress, and — once the
transfer completes — extracts the archive into the canonical
``<repo_root>/models/<id>/`` directory via the shared
``weight_downloader.extract_weights_archive`` helper.

Design (Clean Architecture, fully testable)
-------------------------------------------
Every external collaborator is injected via the constructor, so unit
tests substitute fakes and the use case performs NO direct filesystem /
network access itself:

* ``engine`` — :class:`DownloadEnginePort` (aria2c RPC engine in prod, a
  fake yielding :class:`DownloadProgress` snapshots in tests).
* ``config_port`` — :class:`WeightDownloadConfigPort` returning the
  per-Pack :class:`WeightDownloadConfig` (or ``None`` → error).
* ``extract`` — the shared ``extract_weights_archive`` callable (unzip +
  copy + verify). A fake records its call args in tests.
* ``detect_device`` — the shared ``detect_device_model`` callable
  returning ``"snapdragon_x_elite"`` / ``"snapdragon_x2_elite"``.
* ``repo_root`` / ``data_root`` — path anchors: the ZIP downloads to a
  staging area under ``data_root`` (NOT ``models/<id>/`` directly); the
  extract lands files in ``repo_root/models/<id>/``.

Lifecycle
---------
* :meth:`start` resolves the device + config, picks the device-specific
  ``{url, archive_name, extracted_dir}`` (falling back to
  ``snapdragon_x_elite``), builds ``SourceUrl`` + ``StorageKey``, creates
  a :class:`_WeightDownloadJob` holder (satisfies ``DownloadJobLike``),
  registers it in an in-memory dict, and calls ``engine.start``.
* :meth:`stream` relays ``engine.stream_progress`` snapshots to the
  caller; when the stream ends with a complete final snapshot it runs the
  extract into ``models/<id>/`` and deletes the temp ZIP, so the SSE
  ``done`` frame is only emitted after extraction succeeds. Extraction
  failure surfaces as a raised :class:`QaiError` (→ SSE ``error`` frame).
* :meth:`cancel` delegates to ``engine.cancel`` (idempotent).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from qai.app_builder.application.ports import WeightDownloadConfigPort
from qai.app_builder.domain.errors import AppModelNotFoundError
from qai.platform.download import (
    DownloadEnginePort,
    DownloadProgress,
    SourceUrl,
    StorageKey,
)
from qai.platform.errors import ApplicationError
from qai.platform.ids import IdGenerator

__all__ = [
    "DownloadModelWeightsUseCase",
    "WeightDownloadNotConfiguredError",
    "WeightExtractionError",
]


# Storage category for the staged (temp) weight archive. Kept off
# ``models`` so a half-downloaded ZIP never pollutes the canonical
# ``models/<id>/`` install dir; the extract step is what lands the final
# files there. ``StorageKey.category`` must match ``[a-z][a-z0-9_]{0,31}``.
_STAGING_CATEGORY = "app_builder_weights_tmp"


class WeightDownloadNotConfiguredError(ApplicationError):
    """Raised when a model has no (valid) ``weights.json`` download config."""

    default_code = "app_builder.weights_not_configured"


class WeightExtractionError(ApplicationError):
    """Raised when the downloaded archive fails to extract / verify."""

    default_code = "app_builder.weights_extraction_failed"


class _JobId:
    """Tiny ``.value``-exposing id holder (satisfies ``_JobIdLike``)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


@dataclass(slots=True)
class _WeightDownloadJob:
    """Structural download job satisfying ``DownloadJobLike``.

    The engine only reads ``job.job_id.value`` + ``job.progress``; this
    app_builder-local holder avoids importing any ``model_catalog`` entity
    (context-isolation) while carrying the extra bookkeeping the use case
    needs to run the post-download extract (target model id + staged zip
    path + resolved extract parameters).
    """

    job_id: _JobId
    model_id: str
    archive_path: Path
    model_dir: Path
    extracted_dir: str
    required_files: tuple[str, ...]
    optional_files: tuple[str, ...]
    tag: str
    progress: DownloadProgress = field(
        default_factory=lambda: DownloadProgress(
            bytes_downloaded=0,
            total_bytes=None,
            speed_bps=0.0,
            eta_seconds=None,
        )
    )


class _ExtractCallable(Protocol):
    """Structural type of the shared ``extract_weights_archive`` helper."""

    def __call__(
        self,
        archive_path: Path,
        model_dir: Path,
        *,
        extracted_dir: str,
        required_files: Sequence[str],
        optional_files: Sequence[str] = ...,
        tag: str = ...,
        progress_cb: Callable[..., None] | None = ...,
    ) -> None:
        ...


class DownloadModelWeightsUseCase:
    """Start / stream / cancel a built-in model's weight download."""

    def __init__(
        self,
        *,
        engine: DownloadEnginePort,
        config_port: WeightDownloadConfigPort,
        extract: _ExtractCallable,
        detect_device: Callable[[], str],
        ids: IdGenerator,
        repo_root: Path,
        data_root: Path,
    ) -> None:
        self._engine = engine
        self._config_port = config_port
        self._extract = extract
        self._detect_device = detect_device
        self._ids = ids
        self._repo_root = Path(repo_root)
        self._data_root = Path(data_root)
        self._jobs: dict[str, _WeightDownloadJob] = {}

    # ── start ──────────────────────────────────────────────────────────

    async def start(self, model_id: str) -> str:
        """Resolve config + device, submit the download, return the job id.

        Raises :class:`WeightDownloadNotConfiguredError` when the model has
        no downloadable weights config (the route maps that to a 404-able
        envelope).
        """
        config = self._config_port.get(model_id)
        if config is None:
            raise WeightDownloadNotConfiguredError(
                "app_builder.weights_not_configured",
                f"model {model_id!r} has no downloadable weights config",
            )

        device_model = self._detect_device()
        try:
            device_cfg = config.resolve_device_config(device_model)
        except KeyError as exc:
            raise WeightDownloadNotConfiguredError(
                "app_builder.weights_not_configured",
                f"model {model_id!r}: {exc}",
            ) from exc

        archive_name = device_cfg["archive_name"]
        # Stage the ZIP under <data_root>/<staging>/<archive_name>. This MUST
        # match where the engine actually writes it: the engine resolves the
        # download dir as ``download_root/<StorageKey.category>`` (we inject
        # ``download_root == data_root``), and the file name as
        # ``StorageKey.name``. So the archive lands at
        # ``data_root/app_builder_weights_tmp/<archive_name>`` — the same path
        # ``_finalize`` reads from. (StorageKey.category is constrained to
        # ``[a-z][a-z0-9_]*`` and .name forbids separators, so we cannot nest a
        # per-model sub-dir here; the archive_name is already model+device
        # unique, so distinct models never collide.)
        staging_dir = self._data_root / _STAGING_CATEGORY
        archive_path = staging_dir / archive_name
        model_dir = self._repo_root / "models" / model_id

        job = _WeightDownloadJob(
            job_id=_JobId(self._ids.new_id()),
            model_id=model_id,
            archive_path=archive_path,
            model_dir=model_dir,
            extracted_dir=device_cfg["extracted_dir"],
            required_files=config.required_files,
            optional_files=config.optional_files,
            tag=config.tag,
        )
        self._jobs[job.job_id.value] = job

        source = SourceUrl(value=device_cfg["url"])
        target = StorageKey(category=_STAGING_CATEGORY, name=archive_name)
        await self._engine.start(job, source=source, target=target)
        return job.job_id.value

    # ── stream ─────────────────────────────────────────────────────────

    async def stream(self, job_id: str) -> AsyncIterator[DownloadProgress]:
        """Return an async iterator of progress snapshots for ``job_id``.

        Raising ``AppModelNotFoundError`` *before* the iterator begins lets
        the route surface a clean 404 (no stream committed yet). Once the
        stream starts, extraction runs at end-of-stream and any failure is
        raised as a ``QaiError`` so the SSE layer emits an ``error`` frame.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise AppModelNotFoundError(
                "app_builder.download_job_not_found",
                f"weight-download job {job_id!r} not found",
            )
        return self._iterate(job)

    async def _iterate(
        self, job: _WeightDownloadJob
    ) -> AsyncIterator[DownloadProgress]:
        last: DownloadProgress | None = None
        async for snapshot in self._engine.stream_progress(job):
            last = snapshot
            job.progress = snapshot
            yield snapshot

        # Stream ended — extract only on a genuinely complete transfer so a
        # cancelled / truncated download never lands half a model. The
        # ``done`` SSE frame is emitted by the shared translator AFTER this
        # coroutine returns, i.e. only once extraction has succeeded.
        if last is not None and last.is_complete:
            self._finalize(job)
        # Drop the job from the registry once the stream is done (success OR
        # incomplete/aborted end-of-stream) so the in-memory dict does not grow
        # unbounded across repeated downloads. A failed extract raises before
        # this line, leaving the job for the caller to inspect/retry.
        self._jobs.pop(job.job_id.value, None)

    def _finalize(self, job: _WeightDownloadJob) -> None:
        """Extract the staged ZIP into ``models/<id>/`` then delete it."""
        try:
            self._extract(
                job.archive_path,
                job.model_dir,
                extracted_dir=job.extracted_dir,
                required_files=job.required_files,
                optional_files=job.optional_files,
                tag=job.tag,
            )
        except Exception as exc:  # noqa: BLE001 — normalise to a QaiError
            raise WeightExtractionError(
                "app_builder.weights_extraction_failed",
                f"failed to extract weights for {job.model_id!r}: {exc}",
            ) from exc
        # Best-effort temp cleanup: the extract helper already unlinks the
        # archive on success, but remove any residue defensively.
        try:
            if job.archive_path.is_file():
                job.archive_path.unlink()
        except OSError:
            pass

    # ── cancel ─────────────────────────────────────────────────────────

    async def cancel(self, job_id: str) -> None:
        """Best-effort cancel of an in-flight download (idempotent).

        Cancels the engine transfer, deletes the partial staged archive (so a
        half-downloaded ZIP does not linger under ``data/`` — it is NOT in the
        canonical ``models/<id>/`` dir, so this only reclaims temp space), and
        drops the job from the registry.
        """
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        await self._engine.cancel(job)
        try:
            if job.archive_path.is_file():
                job.archive_path.unlink()
        except OSError:
            pass
