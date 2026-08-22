# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RunAppUseCase`` — orchestrate one model run end-to-end.

Responsibilities:

1. Look up the :class:`AppModelDefinition`; reject disabled / missing.
2. Build a fresh :class:`Run` aggregate (PENDING).
3. Persist the PENDING run, then transition to RUNNING and persist
   again. Publish :class:`RunStartedEvent`.
4. Drive the runner's async iterator: yield each :class:`RunFrame` to
   the caller (typically an SSE presenter). Per-frame run output is
   delivered over the dedicated per-run stream, NOT the event bus, so
   no per-frame event is published here.
5. On normal completion → persist COMPLETED + publish
   :class:`RunCompletedEvent`.
6. On exception → persist FAILED + publish :class:`RunFailedEvent`,
   then re-raise so the caller sees the failure.
7. On caller-initiated cancellation (the consumer stops iterating /
   throws :class:`asyncio.CancelledError`) → persist CANCELLED +
   publish :class:`RunCancelledEvent`.

The use case yields :class:`RunFrame` objects so the interfaces layer
can map directly to SSE without owning state. The terminal status is
written to the repository **before** the iterator returns control — so
``last_results`` queries are immediately consistent.

Optional collaborators (PR-094 §17.5 #11 / #12)
-----------------------------------------------

The constructor accepts three optional collaborators that the production
DI wires from ``Settings.app_builder``:

* ``result_cache`` — :class:`ResultCachePort` adapter retained as
  optional infrastructure (V1 ``api_routes.py`` stats/clear parity,
  surfaced by the admin ``GET /cache/status`` / ``DELETE /cache``
  routes). **The run-button path no longer reads or writes it**: V1's
  "推理" button hard-codes ``options.noCache=true``
  (``useAppBuilder.js:496``) so the V1 backend skips both the cache
  lookup (``api_routes.py:600,603``) and the write-back (``:681``) and
  always runs the model for real. V2 mirrors that — ``execute`` keeps
  ``cached_payload``/``cache_key`` ``None`` so ``_stream`` always drives
  the runner and never writes back. The production wiring still binds an
  LRU adapter from
  :mod:`qai.app_builder.infrastructure.result_cache`; the use case sees
  only the Port surface and leaves it untouched on the run path.
* ``dep_checker`` — :class:`DepCheckerPort` adapter
  used to fan out a per-model ``importlib.find_spec`` probe (and
  optional pip / uv install) before the runner spawns. Failures are
  logged and ignored — the runner subprocess still runs and surfaces
  any genuine ImportError as a normal RUN_FAILED event. The production
  wiring binds the adapter from
  :mod:`qai.app_builder.infrastructure.dep_checker`; the use case sees
  only the Port surface.
* ``manifest_resolver`` — callable ``model_id -> PackManifest | None``
  consumed by :func:`_extract_pack_deps` to read the manifest's
  ``runner.requirements`` file path and derive the dynamic dep list.
  Wired by ``apps/api/_app_builder_di.py`` from the same provider used
  by :class:`ResolveSkillFilesUseCase`.

All three default to ``None``; pre-PR-094 callers see byte-for-byte the
original behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from qai.app_builder.application.ports import (
    AppModelRepositoryPort,
    ArtifactStorePort,
    DepCheckerPort,
    ResultCachePort,
    RunnerPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.errors import (
    AppModelDisabledError,
    AppModelNotFoundError,
)
from qai.app_builder.domain.events import (
    RunCancelledEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
)
from qai.app_builder.domain.run import Run, RunFrame, RunStatus
from qai.app_builder.domain.value_objects import AppModelId, RunId
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

__all__ = ["RunAppUseCase"]

_log = logging.getLogger("qai.app_builder.run_app")


class RunAppUseCase:
    """Orchestrate a single model run, streaming frames as they arrive."""

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        runs: RunRepositoryPort,
        runner: RunnerPort,
        artifact_store: ArtifactStorePort,
        events: EventBus,
        clock: Clock,
        ids: IdGenerator,
        result_cache: ResultCachePort | None = None,
        dep_checker: DepCheckerPort | None = None,
        manifest_resolver: "Callable[[Any], Any | None] | None" = None,
    ) -> None:
        self._app_models = app_models
        self._runs = runs
        self._runner = runner
        self._artifact_store = artifact_store
        self._events = events
        self._clock = clock
        self._ids = ids
        # PR-094 §17.5 #11 / #12 — optional production-only collaborators
        # wired by ``apps/api/_app_builder_di.py`` from
        # ``Settings.app_builder``. ``None`` keeps tests / fixtures byte-
        # for-byte compatible with the pre-PR-094 constructor.
        self._result_cache = result_cache
        self._dep_checker = dep_checker
        self._manifest_resolver = manifest_resolver

    async def execute(
        self,
        *,
        model_id: AppModelId,
        inputs: dict[str, object],
    ) -> AsyncIterator[RunFrame]:
        """Return an async iterator yielding :class:`RunFrame` chunks.

        The returned iterator is also responsible for advancing the
        :class:`Run` state machine. Callers MUST iterate to completion
        OR cancel the iterator (e.g. via ``aclose()``); otherwise the
        run is left in ``RUNNING`` (callers should treat that as a
        timeout and reconcile via :class:`CancelRunUseCase`).
        """
        # Fetch model up front so caller sees errors before any state is
        # written.
        model = await self._app_models.get(model_id)
        if not model.is_runnable:
            raise AppModelDisabledError(
                message=f"app model {model_id} is disabled",
                details={"model_id": str(model_id)},
            )

        # PR-094 §17.5 #11 — fire-and-forget dynamic dep probe + install.
        # The check is keyed by ``model_id`` so subsequent invocations
        # within the cool-down window short-circuit. Errors here are
        # logged but never surfaced — the runner spawn that follows will
        # raise the genuine ImportError if a missing dep was not auto-
        # installed in time.
        if self._dep_checker is not None:
            declared = _extract_pack_deps(model, self._manifest_resolver)
            if declared:
                try:
                    self._dep_checker.ensure(str(model_id), declared)
                except Exception as exc:  # noqa: BLE001 — dep probe must not break run
                    _log.warning(
                        "dep_checker.ensure failed for model %r: %s",
                        model_id,
                        exc,
                    )

        run = Run(
            id=RunId(value=self._ids.new_id()),
            model_id=model_id,
            inputs=dict(inputs),
            created_at=self._clock.now(),
        )
        await self._runs.save(run)

        # V1 parity (AGENTS.md 判据 2) — the App Builder "推理" button is a
        # *real* inference every time. V1 hard-codes ``options.noCache=true``
        # on the run-button path (``useAppBuilder.js:496``); the V1 backend
        # then skips the ``result_cache`` lookup (``api_routes.py:600,603``)
        # AND the write-back (``:681``), so the runner ALWAYS executes on the
        # NPU. The "switch back to a previously-run model and still see the
        # last result" UX in V1 is powered by persisted history
        # (``last_results.json`` ↔ V2 ``RunRepositoryPort.list_by_model`` /
        # ``get_last_for_model``, surfaced by ``GET /runs`` and hydrated by the
        # store's ``fetchHistory``), NOT by ``result_cache``.
        #
        # We therefore DO NOT consult ``result_cache`` on the run path:
        # ``cached_payload`` stays ``None`` so ``_stream`` always drives the
        # real runner, and ``cache_key`` stays ``None`` so ``_stream`` also
        # skips the write-back — byte-for-byte the V1 ``noCache`` behaviour.
        # The ``ResultCachePort`` collaborator + the admin
        # ``GET /cache/status`` / ``DELETE /cache`` routes remain wired as
        # optional infrastructure (V1 stats/clear parity); they are simply
        # never read or written by the run-button flow.
        cache_key: str | None = None
        cached_payload: list[RunFrame] | None = None
        return self._stream(run, model, cache_key, cached_payload)

    # ------------------------------------------------------------------
    # internal generator
    # ------------------------------------------------------------------
    async def _stream(
        self,
        run: Run,
        model,  # AppModelDefinition; avoid circular hint
        cache_key: str | None = None,
        cached_payload: list[RunFrame] | None = None,
    ) -> AsyncIterator[RunFrame]:
        run = run.start(now=self._clock.now())
        await self._runs.save(run)
        await self._events.publish(
            RunStartedEvent(
                run_id=run.id,
                model_id=run.model_id,
                started_at=run.started_at,  # type: ignore[arg-type]
            )
        )
        # Mark as STREAMING right away — the runner is allowed to
        # produce zero frames; we still consider it "streaming-capable".
        run = run.begin_streaming()
        await self._runs.save(run)

        captured_frames: list[RunFrame] = []
        # PR-F1 (F-15) — observe runner-emitted ``error`` frames so the
        # Run aggregate can record both ``error_message`` AND a
        # structured ``error_code`` (e.g. ``"WEIGHTS_NOT_INSTALLED"``).
        # Pre-PR-F1 the run was silently transitioned to ``COMPLETED``
        # whenever the runner subprocess exited cleanly after emitting
        # an error frame (V1 ``_UserError`` in the runner script);
        # the SSE consumer saw the error frame but the REST DTO showed
        # ``status=completed`` / ``error_message=None``. Now we promote
        # such runs to ``FAILED`` with the runner's own message + code.
        runner_error_code: str | None = None
        runner_error_message: str | None = None
        # 缺口 #6 — pure-inference latency from the runner's ``metrics``
        # NDJSON event (``latencyMs``). V1 parity: ``useAppBuilder.js:601``
        # records ``run.metrics.latencyMs`` from the ``metrics`` event and
        # persists it (``last_results.json``); V2 lands it on the Run
        # aggregate before ``complete()`` so it survives a restart and the
        # history "Inference" column shows the real inference time, not the
        # end-to-end duration. Last ``metrics`` event with a valid latency
        # wins (the runner is expected to emit one near the end).
        runner_latency_ms: float | None = None
        try:
            if cached_payload is not None:
                # V1 parity — DEAD on the run-button path: ``execute`` always
                # passes ``cached_payload=None`` so the runner runs for real
                # (see the rationale block in ``execute``). This replay branch
                # is retained only so a future non-run-button caller could opt
                # into cache replay by supplying a payload explicitly; the App
                # Builder "推理" button never does.
                _log.debug(
                    "result_cache replay run=%s key=%s",
                    run.id,
                    cache_key,
                )
                for frame in cached_payload:
                    # Per-frame ``RunFrameEvent`` is intentionally NOT published
                    # to the event bus (see the ``async for`` branch below for
                    # the rationale). The run frames reach the front-end over
                    # the dedicated per-run stream (RunStreamBroadcaster), not
                    # the bus.
                    yield frame
            else:
                async for frame in self._runner.execute(
                    run, model, artifact_store=self._artifact_store
                ):
                    captured_frames.append(frame)
                    # Inspect runner ``error`` frames to surface the
                    # structured code on the terminal Run aggregate.
                    # The first error wins (the runner is expected to
                    # emit at most one ``error`` event then exit).
                    if (
                        runner_error_code is None
                        and isinstance(frame.payload, dict)
                        and frame.payload.get("event") == "error"
                    ):
                        code_val = frame.payload.get("code")
                        msg_val = frame.payload.get("message")
                        if isinstance(code_val, str) and code_val.strip():
                            runner_error_code = code_val
                        if isinstance(msg_val, str) and msg_val.strip():
                            runner_error_message = msg_val
                    # 缺口 #6 — capture inference latency from the runner's
                    # ``metrics`` event so it can be persisted on the Run
                    # before ``complete()`` (V1 ``useAppBuilder.js:601-606``).
                    if (
                        isinstance(frame.payload, dict)
                        and frame.payload.get("event") == "metrics"
                    ):
                        lat_val = frame.payload.get("latencyMs")
                        if (
                            isinstance(lat_val, (int, float))
                            and not isinstance(lat_val, bool)
                            and lat_val >= 0
                        ):
                            runner_latency_ms = float(lat_val)
                    # Per-frame ``RunFrameEvent`` is intentionally NOT published
                    # to the in-process event bus. No production subscriber
                    # consumes per-frame run events — the run output reaches the
                    # front-end over the dedicated per-run stream
                    # (``RunStreamBroadcaster`` → ``/runs/{id}/stream`` SSE),
                    # which tees the runner iterator directly. The only bus
                    # subscriber that matched ``app_builder.run_frame`` was the
                    # global ``/api/events`` notification SSE, which dropped it.
                    # Publishing per frame only floods that bounded queue (the
                    # historical ``events.backpressure`` log-spam). The terminal
                    # run lifecycle events (started / completed / failed /
                    # cancelled) below ARE low-frequency notifications and stay
                    # on the bus.
                    yield frame
        except asyncio.CancelledError:
            run = run.cancel(now=self._clock.now(), reason="cancelled")
            await self._runs.save(run)
            await self._events.publish(
                RunCancelledEvent(
                    run_id=run.id,
                    model_id=run.model_id,
                    finished_at=run.finished_at,  # type: ignore[arg-type]
                    reason="cancelled",
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 — converted into FAILED state
            message = str(exc) or type(exc).__name__
            run = run.fail(
                now=self._clock.now(),
                message=message,
                code=runner_error_code,
            )
            await self._runs.save(run)
            await self._events.publish(
                RunFailedEvent(
                    run_id=run.id,
                    model_id=run.model_id,
                    finished_at=run.finished_at,  # type: ignore[arg-type]
                    error_message=message,
                )
            )
            raise

        # Normal termination.
        if run.status != RunStatus.COMPLETED:
            # State-Truth-First (§🔴) — cancel-overwrite guard. A concurrent
            # ``CancelRunUseCase`` may have already transitioned the persisted
            # Run to CANCELLED (it writes the DB row directly) while the runner
            # happened to emit a clean ``done`` before its cooperative cancel
            # check fired. Our in-memory ``run`` object never saw that cancel,
            # so calling ``run.complete()``/``fail()`` here would overwrite the
            # authoritative CANCELLED row back to COMPLETED — exactly the
            # divergence that made a "cancelled" run show as "completed" in the
            # history list. Re-read the persisted status and bail out without
            # overwriting when it is already terminal (cancelled/failed/…).
            try:
                persisted = await self._runs.get(run.id)
            except Exception:  # noqa: BLE001 — repo miss → proceed as before
                persisted = None
            if persisted is not None and persisted.status in (
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.COMPLETED,
            ):
                # Honour the already-persisted terminal state (typically a
                # user cancel). Do not republish a competing lifecycle event.
                return
            # PR-F1 (F-15) — if the runner streamed an ``error`` frame
            # (e.g. ``WEIGHTS_NOT_INSTALLED`` from the V1 ``_UserError``
            # protocol) the subprocess will have exited cleanly, so we
            # land here without raising. Promote the run to FAILED so
            # the REST DTO mirrors the SSE error frame instead of
            # silently reporting ``completed``.
            if runner_error_code is not None or runner_error_message is not None:
                fail_msg = runner_error_message or runner_error_code or "run failed"
                run = run.fail(
                    now=self._clock.now(),
                    message=fail_msg,
                    code=runner_error_code,
                )
                await self._runs.save(run)
                await self._events.publish(
                    RunFailedEvent(
                        run_id=run.id,
                        model_id=run.model_id,
                        finished_at=run.finished_at,  # type: ignore[arg-type]
                        error_message=fail_msg,
                    )
                )
                return
            # 缺口 #6 — stamp the runner-reported inference latency onto the
            # Run before completing so it persists with the COMPLETED row.
            if runner_latency_ms is not None:
                run = run.with_inference_latency(runner_latency_ms)
            run = run.complete(now=self._clock.now())
            await self._runs.save(run)
            await self._events.publish(
                RunCompletedEvent(
                    run_id=run.id,
                    model_id=run.model_id,
                    finished_at=run.finished_at,  # type: ignore[arg-type]
                    artifact_count=len(run.artifacts),
                )
            )
            # V1 parity — the run-button path skips the cache write-back
            # exactly like V1's ``noCache`` flow (``api_routes.py:681`` is
            # skipped). ``execute`` always passes ``cache_key=None`` on this
            # path, so the guard below is always False and ``put`` never
            # fires. Retained only for the explicit-cache-key caller noted
            # above; the App Builder "推理" button never writes the cache.
            if (
                self._result_cache is not None
                and cache_key is not None
                and cached_payload is None
                and captured_frames
            ):
                try:
                    await self._result_cache.put(cache_key, captured_frames)
                except Exception as exc:  # noqa: BLE001 — cache must not break run
                    _log.warning(
                        "result_cache.put failed for run %r: %s",
                        run.id,
                        exc,
                    )


# ---------------------------------------------------------------------------
# Module-level helpers — kept private so callers cannot rely on the shape
# of the inputs bag for cache-key extraction. Retained as the documented
# reference for the ``result_cache`` key shape (see
# ``infrastructure/command_resolver/registry.py``); the run-button path no
# longer derives a cache key (V1 ``noCache`` parity), but these stay so the
# optional cache infrastructure + any future explicit-cache caller has a
# single source of truth for the ``(variant_id, params)`` extraction.
# ---------------------------------------------------------------------------
def _extract_variant_id(inputs: dict[str, object]) -> str | None:
    """Return ``inputs['variant_id']`` as a string or ``None``."""
    v = inputs.get("variant_id")
    if isinstance(v, str) and v:
        return v
    return None


def _extract_params(inputs: dict[str, object]) -> dict[str, Any] | None:
    """Return the ``params`` sub-dict (used for cache-key stability)."""
    p = inputs.get("params")
    if isinstance(p, dict):
        return p
    return None


def _extract_pack_deps(
    model: Any,
    manifest_resolver: Callable[[Any], Any | None] | None,
) -> list[str]:
    """Best-effort extraction of declared Pack runtime deps.

    Strategy (in order):

    1. If ``manifest_resolver`` is wired and returns a manifest, parse
       its ``runner.requirements`` (a path to ``requirements.txt``
       relative to the Pack root). When the file exists, return the
       list of non-comment lines verbatim. When the manifest is absent
       or the requirements path is empty, fall through.
    2. Probe the :class:`AppModelDefinition` aggregate for an inline
       ``runtime_requirements`` / ``deps`` / ``requirements`` attribute.
       The current ``AppModelDefinition`` (PR-034) does not carry such
       a field; this branch exists so a forward-compatible aggregate
       revision can ship a typed ``deps`` field without re-wiring this
       use case.
    3. Return ``[]`` — nothing to probe.
    """
    if manifest_resolver is not None:
        try:
            manifest = manifest_resolver(model.id)
        except Exception:  # noqa: BLE001 -- manifest lookup must not break run
            manifest = None
        if manifest is not None:
            req_path_str = getattr(
                getattr(manifest, "runner", None), "requirements", ""
            )
            if isinstance(req_path_str, str) and req_path_str.strip():
                from pathlib import Path as _Path

                req_path = _Path(req_path_str)
                if req_path.is_file():
                    try:
                        text = req_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        _log.warning(
                            "failed to read pack requirements %s: %s",
                            req_path,
                            exc,
                        )
                        text = ""
                    lines: list[str] = []
                    for raw in text.splitlines():
                        line = raw.split("#", 1)[0].strip()
                        if not line or line.startswith("-"):
                            continue
                        lines.append(line)
                    if lines:
                        return lines
    for attr in ("runtime_requirements", "deps", "requirements"):
        v = getattr(model, attr, None)
        if isinstance(v, (list, tuple)) and v:
            return [str(x) for x in v if x]
    return []


# Re-exported for tests that want to spell out the missing-model case.
__all__ += ["AppModelNotFoundError"]
