# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — voice preference + feedback + benchmark routes.

Covers the voice-preference GET/PUT pair, the sticky-worker preload
trigger, the run feedback intake and the benchmark schedule + status
pair. Grouped together as the "post-run user signal" surface. Handler
bodies are byte-for-byte identical to the pre-split module.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ._dto import (
    BenchmarkRequestBody,
    BenchmarkResponseBody,
    BenchmarkStatusResponse,
    FeedbackRequestBody,
    FeedbackResponseBody,
    PreloadRequest,
    PreloadResultResponse,
    VoicePreferenceRequest,
    VoicePreferenceResponse,
    _validate_run_id,
)

from qai.app_builder.application.use_cases.run_benchmark import (
    GetBenchmarkUseCase,
    RunBenchmarkCommand,
    RunBenchmarkUseCase,
)
from qai.app_builder.application.use_cases.submit_feedback import (
    SubmitFeedbackCommand,
    SubmitFeedbackUseCase,
)
from qai.app_builder.application.use_cases.voice_preference import (
    GetVoicePreferenceUseCase,
    SetVoicePreferenceUseCase,
)
from qai.app_builder.domain.value_objects import AppModelId
from qai.platform.errors import NotFoundError, ValidationError
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

logger = get_logger(__name__)


def register(router: APIRouter, *, container: "Container") -> None:
    """Mount the voice / feedback / benchmark routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    # ---- voice preference -------------------------------------------------

    @router.get("/voice-preference", response_model=VoicePreferenceResponse)
    async def get_voice_pref() -> VoicePreferenceResponse:
        uc: GetVoicePreferenceUseCase = _services().get_voice_pref_use_case
        pref = await uc.execute()
        return VoicePreferenceResponse(
            enabled=pref.enabled,
            preferred_model_id=str(pref.preferred_model_id)
            if pref.preferred_model_id is not None
            else None,
            preferred_variant_id=pref.preferred_variant_id,
        )

    @router.put("/voice-preference", response_model=VoicePreferenceResponse)
    async def set_voice_pref(
        body: VoicePreferenceRequest,
    ) -> VoicePreferenceResponse:
        uc: SetVoicePreferenceUseCase = _services().set_voice_pref_use_case
        pref = await uc.execute(
            enabled=body.enabled,
            preferred_model_id=body.preferred_model_id,
            preferred_variant_id=body.preferred_variant_id,
        )
        return VoicePreferenceResponse(
            enabled=pref.enabled,
            preferred_model_id=str(pref.preferred_model_id)
            if pref.preferred_model_id is not None
            else None,
            preferred_variant_id=pref.preferred_variant_id,
        )

    # ---- 3. voice-input/preload ---------------------------------------
    @router.post(
        "/voice-input/preload",
        response_model=PreloadResultResponse,
    )
    async def voice_input_preload(
        body: PreloadRequest | None = None,
    ) -> PreloadResultResponse:
        uc = _services().preload_voice_input_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="preload use case not wired")
        # V1 parity (``api_routes.py:976`` reads ``modelId`` from the request
        # body): the chat toolbar passes the currently-selected engine so the
        # warm-up is parameter-driven — picking Whisper warms Whisper, picking
        # Zipformer warms Zipformer (both reach "ready" independently since the
        # sticky worker is multi-model). When the body is omitted (startup
        # warm-up), the use case falls back to the persisted preference.
        req_model_id = body.model_id if body is not None else None
        req_variant_id = body.variant_id if body is not None else None
        r = await uc.execute(
            model_id=req_model_id,
            variant_id=req_variant_id,
        )
        # V1 parity (``api_routes.py:1044`` preload calls
        # ``get_or_create_worker``): when the use case acknowledges a load
        # ("loaded"), actually fire the resident warm-load in the
        # background so ``worker/status.loaded_models`` reflects the model
        # and the UI voice-engine dot flips "loading" → "ready". The
        # earlier behaviour returned "loaded" without loading anything, so
        # the dot span forever. Fire-and-forget (held by the TaskRegistry)
        # so the route returns immediately — the front-end polls
        # ``worker/status`` for the real residency state (State-Truth).
        if r.status == "loaded" and r.model_id:
            try:
                from apps.api.lifespan import warm_load_model_into_host

                async def _warm() -> None:
                    await warm_load_model_into_host(
                        container,
                        model_id=str(r.model_id),
                        variant_id=r.variant_id,
                    )

                _registry = getattr(_services(), "background_tasks", None)
                if _registry is not None:
                    _registry.spawn(
                        _warm(), name=f"voice-preload-{r.model_id}"
                    )
                else:
                    asyncio.create_task(_warm())
            except Exception:  # noqa: BLE001 — preload must never 500
                logger.info(
                    "app_builder.voice_preload_warm_dispatch_failed",
                    extra={"model_id": str(r.model_id)},
                )
        return PreloadResultResponse(
            status=r.status,
            model_id=r.model_id,
            variant_id=r.variant_id,
            detail=r.detail,
        )

    # ---- 8. feedback --------------------------------------------------
    @router.post(
        "/feedback",
        response_model=FeedbackResponseBody,
        status_code=202,
    )
    async def submit_feedback(
        body: FeedbackRequestBody,
    ) -> FeedbackResponseBody:
        from qai.app_builder.domain.errors import RunNotFoundError as _RNF

        services = _services()
        uc: SubmitFeedbackUseCase | None = getattr(
            services, "submit_feedback_use_case", None
        )
        if uc is None:
            raise HTTPException(
                status_code=503,
                detail="feedback use case not wired",
            )
        # ``run_id`` is optional on the wire (legacy frontend posts
        # ``run_id=None`` for free-form site-wide feedback); persistence
        # however requires a Run anchor. Reject the legacy "no run_id"
        # variant with 400 — the wire shape is unchanged because the
        # field was always nullable, and the new behaviour is strictly
        # more correct than silently dropping the row.
        if body.run_id is None or not body.run_id.strip():
            raise HTTPException(
                status_code=400,
                detail="run_id is required",
            )
        if body.rating is None:
            raise HTTPException(
                status_code=400,
                detail="rating is required",
            )
        rid = _validate_run_id(body.run_id)
        command = SubmitFeedbackCommand(
            run_id=rid,
            rating=int(body.rating),
            text=body.text or "",
            extra=dict(body.extra),
        )
        try:
            persisted = await uc.execute(command)
        except _RNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FeedbackResponseBody(feedback_id=persisted.id)

    # ---- 11. benchmark ------------------------------------------------
    @router.post(
        "/benchmark",
        response_model=BenchmarkResponseBody,
        status_code=202,
    )
    async def run_benchmark(body: BenchmarkRequestBody) -> BenchmarkResponseBody:
        services = _services()
        uc: RunBenchmarkUseCase | None = getattr(
            services, "run_benchmark_use_case", None
        )
        if uc is None:
            raise HTTPException(
                status_code=503,
                detail="benchmark use case not wired",
            )
        try:
            mid = AppModelId(value=body.model_id)
        except ValueError as exc:
            raise ValidationError(
                "app_builder.app_model_id_invalid",
                str(exc),
                field_errors={"model_id": [str(exc)]},
            ) from exc
        command = RunBenchmarkCommand(
            model_id=mid,
            iterations=int(body.iterations),
            warmup=0,
            inputs={},
        )
        # Persist the scheduled row synchronously so the route can
        # return an id immediately, then dispatch the harness as a
        # detached task. Errors during the harness are persisted on
        # the row's ``status="failed"`` so clients polling
        # ``GET /benchmark/{id}`` see the terminal state.
        record = await uc.schedule(command)

        async def _drive() -> None:
            try:
                await uc.run_to_completion(record.id)
            except Exception:  # noqa: BLE001 — already persisted as FAILED
                logger.info(
                    "app_builder.benchmark_drive_failed",
                    extra={"benchmark_id": record.id},
                )

        # R-3 — retain a strong ref via the DI-wired TaskRegistry so the
        # fire-and-forget drive task is not GC'd mid-flight and gets
        # cancelled on app shutdown. Fall back to a bare ``create_task``
        # for hand-built test namespaces that omit the field.
        _registry = getattr(_services(), "background_tasks", None)
        if _registry is not None:
            _registry.spawn(_drive(), name=f"benchmark-drive-{record.id}")
        else:
            asyncio.create_task(_drive())
        return BenchmarkResponseBody(benchmark_id=record.id)

    # ---- 11b. benchmark status (S9 close — paired with POST) ----------
    @router.get(
        "/benchmark/{benchmark_id}",
        response_model=BenchmarkStatusResponse,
    )
    async def get_benchmark(benchmark_id: str) -> BenchmarkStatusResponse:
        services = _services()
        uc: GetBenchmarkUseCase | None = getattr(
            services, "get_benchmark_use_case", None
        )
        if uc is None:
            raise HTTPException(
                status_code=503,
                detail="benchmark use case not wired",
            )
        try:
            record = await uc.execute(benchmark_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return BenchmarkStatusResponse(
            id=record.id,
            model_id=str(record.model_id),
            iterations=record.iterations,
            warmup=record.warmup,
            status=record.status,
            stats=dict(record.stats),
            raw_latencies_ms=list(record.raw_latencies_ms),
            error_message=record.error_message,
            created_at=record.created_at.isoformat(),
            finished_at=(
                record.finished_at.isoformat()
                if record.finished_at is not None
                else None
            ),
        )
