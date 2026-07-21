# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sticky-worker-backed :class:`RunnerPort` (PR-302 wiring).

This adapter is the long-missing PR-302 integration: it makes every App
Builder run **prefer a long-running, resident model worker** and only
fall back to the per-run one-shot subprocess
(:class:`ProcessBackedAppRunner`) when the sticky worker is unavailable.

Behaviour parity with V1
------------------------
Mirrors the two-layer structure of V1
``backend/app_builder/runners/python_script.py:56-135``:

* ``run_pack`` tries ``_run_pack_sticky`` first (``get_or_create_worker``
  + ``worker.execute_run`` -- the model stays resident on the NPU and is
  reused across runs), and only falls back to ``_run_pack_oneshot`` when
  the sticky path raises ``_StickyWorkerUnavailable`` / any error.

The decisive win: with a resident worker the model is **not** destroyed
after every inference, so the NPU context is never torn down per-run.
That eliminates the repeated ``model_destroy`` + ``NPU Error 0x200``
churn the one-shot path causes (V1 has no such error precisely because
the worker is sticky).

Architecture
------------
* :class:`StickyBackedAppRunner` is an infrastructure adapter satisfying
  :class:`qai.app_builder.application.ports.RunnerPort`. It owns the
  :class:`StickyWorkerHost` reference, a :class:`StickyLoadResolver`
  (``(Run, AppModelDefinition) -> LoadModelRequest | None``) and the
  one-shot ``fallback`` runner.
* The host's ``load_model`` / ``execute_run`` are infrastructure-side
  calls, so wrapping them here keeps the application layer's
  :class:`StickyWorkerSnapshotPort` (alive / is_loaded only) one-way and
  unchanged.

NPU serialisation
-----------------
Both the sticky and one-shot paths serialise on the module-level
``_npu_lock`` from :mod:`process_runner` so only one inference occupies
the NPU at a time (V1 ``runner._npu_lock`` parity). The sticky path
holds the lock for the *load + run* span; the one-shot path holds it for
the subprocess span (it already acquires the same lock internally).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from qai.app_builder.application.ports import ArtifactStorePort, RunnerPort
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.run import Run, RunFrame

from .input_artifact_resolver import resolve_input_artifact_paths
from .process_runner import (
    acquire_npu_lock_with_queue_frames,
    release_npu_lock_and_dequeue,
)
from .sticky_load_resolver import StickyLoadResolver
from .sticky_worker import (
    LoadModelRequest,
    RunRequest,
    StickyWorkerHost,
    WorkerEvent,
)

__all__ = ["StickyBackedAppRunner"]

_log = logging.getLogger("qai.app_builder.infrastructure.sticky_runner")


class _StickyUnavailableError(RuntimeError):
    """Internal sentinel: the sticky path cannot serve this run; fall back."""


class StickyBackedAppRunner:
    """RunnerPort that prefers a resident worker, falling back to one-shot.

    Parameters
    ----------
    host_provider:
        Zero-arg callable returning the live :class:`StickyWorkerHost`
        (or ``None``). Read **lazily on every run** rather than captured
        at construction time so the runner -- wired into ``RunAppUseCase``
        when the container is built -- picks up the host the lifespan hook
        spawns *afterwards* (State-Truth-First: read the live resource,
        not a build-time snapshot). When the provider returns ``None`` /
        a dead host the runner degrades transparently to ``fallback``.
    fallback:
        The one-shot :class:`RunnerPort` (typically
        :class:`ProcessBackedAppRunner`) used when the sticky worker is
        absent / dead / cannot load the requested model.
    load_resolver:
        Builds a :class:`LoadModelRequest` from ``(Run, model)``. Returns
        ``None`` when the model has no registered runner spec -- in which
        case the sticky path is skipped and ``fallback`` runs (which then
        emits ``no_command`` if it too has no spec, preserving PR-045
        behaviour).
    """

    __slots__ = ("_fallback", "_host_provider", "_resolver", "_blobs_dir")

    def __init__(
        self,
        *,
        host_provider: Callable[[], StickyWorkerHost | None],
        fallback: RunnerPort,
        load_resolver: StickyLoadResolver,
        blobs_dir: "Path | None" = None,
    ) -> None:
        self._host_provider = host_provider
        self._fallback = fallback
        self._resolver = load_resolver
        self._blobs_dir = blobs_dir

    def execute(
        self,
        run: Run,
        model: AppModelDefinition,
        *,
        artifact_store: ArtifactStorePort,
    ) -> AsyncIterator[RunFrame]:
        return self._stream(run, model, artifact_store)

    async def _stream(
        self,
        run: Run,
        model: AppModelDefinition,
        artifact_store: ArtifactStorePort,
    ) -> AsyncIterator[RunFrame]:
        host = self._host_provider()
        if host is None or not host.alive:
            # No resident worker: preserve the one-shot path verbatim.
            async for frame in self._fallback.execute(
                run, model, artifact_store=artifact_store
            ):
                yield frame
            return

        load_request = self._resolver(run, model)
        if load_request is None:
            # No runner spec for this model: let the fallback handle it
            # (it will emit ``no_command`` for an unregistered model,
            # PR-045 parity).
            async for frame in self._fallback.execute(
                run, model, artifact_store=artifact_store
            ):
                yield frame
            return

        # Try the sticky path. Any failure before the first event is
        # yielded means we can safely fall back to the one-shot path
        # without the consumer having seen partial output. Once we have
        # started yielding sticky frames we cannot transparently restart,
        # so a mid-stream worker death is surfaced as an ``error`` frame
        # (the use case promotes it to FAILED -- parity with the one-shot
        # subprocess dying mid-run).
        fell_back = False
        try:
            async for frame in self._run_sticky(
                run, model, load_request, host
            ):
                yield frame
        except _StickyUnavailableError as exc:
            fell_back = True
            _log.info(
                "sticky worker unavailable for run %s (%s); "
                "falling back to one-shot",
                run.id,
                exc,
            )

        if fell_back:
            async for frame in self._fallback.execute(
                run, model, artifact_store=artifact_store
            ):
                yield frame

    async def _run_sticky(
        self,
        run: Run,
        model: AppModelDefinition,
        load_request: LoadModelRequest,
        host: StickyWorkerHost,
    ) -> AsyncIterator[RunFrame]:
        """Load (if needed) + run on the resident worker, yielding RunFrames.

        Raises :class:`_StickyUnavailableError` *before* yielding any frame
        to signal the caller it should fall back to the one-shot path.
        After the first frame is yielded, errors surface as ``error``
        RunFrames.
        """
        run_id = str(run.id)
        model_id = str(model.id)
        sequence = 0

        # Fast-path detection (V1 ``will_reuse``): is this exact model +
        # variant already resident? Drives the ``preparing`` hint so the
        # UI shows "model_cached" vs "loading_model" (V1 parity).
        already_loaded = host.is_loaded(model_id, load_request.variant_id)

        # Acquire the NPU lock for the whole load + run span so a concurrent
        # run (or the voice warm-up) waits politely (V1 ``_npu_lock``). While
        # waiting, emit ``queued`` frames carrying the wait position so a
        # second concurrent run shows "排队第 N 位" instead of silently
        # hanging in preparing (G4 / V1 ``runner.py:160-243`` parity). The
        # helper returns holding the lock; we release it in ``finally``.
        queue_gen = acquire_npu_lock_with_queue_frames(
            run_id, sequence_start=sequence
        )
        try:
            async for queued_frame in queue_gen:
                yield queued_frame
                sequence += 1
        finally:
            await queue_gen.aclose()
        # Lock is held now (the helper only returns normally after acquiring).
        try:
            # ---- load (or fast-path confirm) ----
            try:
                if already_loaded:
                    yield RunFrame(
                        sequence=sequence,
                        payload={
                            "event": "status",
                            "state": "preparing",
                            "hint": "model_cached",
                            "runId": run_id,
                        },
                    )
                    sequence += 1
                    # Touch / confirm the cached entry.
                    await host.load_model(load_request)
                else:
                    yield RunFrame(
                        sequence=sequence,
                        payload={
                            "event": "status",
                            "state": "preparing",
                            "hint": "loading_model",
                            "runId": run_id,
                        },
                    )
                    sequence += 1
                    did_load = await host.load_model(load_request)
                    if did_load:
                        yield RunFrame(
                            sequence=sequence,
                            payload={
                                "event": "status",
                                "state": "preparing",
                                "hint": "model_loaded",
                                "runId": run_id,
                            },
                        )
                        sequence += 1
            except _StickyUnavailableError:
                raise
            except Exception as exc:  # degrade to fallback
                # Load failed. We have only emitted preparing/status frames
                # so the consumer can still be recovered by the one-shot
                # fallback -- signal unavailability. The fallback re-emits
                # its own started/status frames, acceptable and mirrors V1
                # which also yields a preparing hint before falling back.
                raise _StickyUnavailableError(
                    f"load_model failed: {type(exc).__name__}: {exc}"
                ) from exc

            if host.state != "ready":
                raise _StickyUnavailableError(
                    f"worker state is {host.state!r}, not ready"
                )

            # ---- run ----
            inputs, params, options = _split_run_inputs(run.inputs)
            # Resolve logical upload paths (``uploads/audio/…``) to absolute
            # physical paths so the resident worker's runner can open the
            # file (it only anchors relative paths against repoRoot/packDir/
            # cwd, none of which is the data blob root). See
            # ``input_artifact_resolver`` for the V1-parity rationale.
            inputs = resolve_input_artifact_paths(
                inputs, blobs_dir=self._blobs_dir
            )
            run_request = RunRequest(
                run_id=run_id,
                model_id=model_id,
                inputs=inputs,
                params=params,
                options=options,
            )
            # A synthetic ``started`` frame so the SSE consumer's frame
            # ordering matches the one-shot path (which emits ``started``
            # with the subprocess pid; the resident worker has no per-run
            # pid so we omit it).
            yield RunFrame(
                sequence=sequence,
                payload={"event": "started", "runId": run_id},
            )
            sequence += 1

            try:
                async for ev in host.execute_run(run_request):
                    yield RunFrame(
                        sequence=sequence,
                        payload=_worker_event_to_payload(ev, run_id),
                    )
                    sequence += 1
            except Exception as exc:  # noqa: BLE001 — surface as error frame
                # Worker crashed mid-run. We have already streamed frames,
                # so we cannot transparently fall back; surface an error
                # frame the use case promotes to FAILED (parity with the
                # one-shot subprocess dying mid-run).
                _log.warning(
                    "sticky worker execute_run failed for run %s: %s",
                    run_id,
                    exc,
                )
                yield RunFrame(
                    sequence=sequence,
                    payload={
                        "event": "error",
                        "code": "WORKER_DIED",
                        "message": f"Sticky worker died during run: {exc}",
                        "runId": run_id,
                    },
                )
                sequence += 1
        finally:
            # Release the lock AND remove ourselves from the wait queue so
            # the next waiter advances to position 0 (G4). The holder stays
            # enqueued during load+run so waiters count it as "1 ahead".
            await release_npu_lock_and_dequeue(run_id)


def _worker_event_to_payload(ev: WorkerEvent, run_id: str) -> dict[str, Any]:
    """Map a :class:`WorkerEvent` onto the canonical RunFrame payload.

    The one-shot path emits ``{"event": <type>, ...}`` payloads decoded
    from the runner_protocol v3.1 NDJSON stream (see
    ``process_runner._decode_stdout_line``). The resident worker already
    decodes the same protocol into :class:`WorkerEvent` envelopes, so we
    only need to re-key ``type`` -> ``event`` and carry the payload
    through verbatim, preserving the wire shape the SSE / use-case layer
    expects (``status / progress / metrics / result / done / error /
    log``).
    """
    payload: dict[str, Any] = {"event": ev.type}
    for key, value in ev.payload.items():
        if key == "type":
            continue
        payload[key] = value
    payload.setdefault("runId", run_id)
    return payload


def _split_run_inputs(
    run_inputs: dict[str, object],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Project :attr:`Run.inputs` into ``(inputs, params, options)``.

    ``Run.inputs`` bundles ``variant_id`` / ``params`` / user inputs for
    cache-key stability (see ``command_resolver._split_run_inputs`` and
    ``run_app._extract_*``). The persistent worker's ``op:run`` payload
    keeps the same three sub-dicts the V1 ``execute_run`` sent
    (``inputs`` / ``params`` / ``options``); ``variant_id`` is consumed at
    load time, not run time, so it is dropped here.
    """
    inputs_payload: dict[str, Any] = {}
    params_payload: dict[str, Any] = {}
    options_payload: dict[str, Any] = {}
    for key, value in (run_inputs or {}).items():
        if key == "variant_id":
            continue
        if key == "params":
            if isinstance(value, dict):
                params_payload = dict(value)
            continue
        if key == "options":
            if isinstance(value, dict):
                options_payload = dict(value)
            continue
        inputs_payload[str(key)] = value
    return inputs_payload, params_payload, options_payload
