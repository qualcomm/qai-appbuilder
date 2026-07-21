# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer bridge: GoMaster External Auto-Optimize job controller.

Builds the :class:`~qai.model_builder.infrastructure.gomaster_external_optimize_adapter.GomasterExternalOptimizeAdapter`
(implementing :class:`~qai.model_builder.application.gomaster_external_optimize.GomasterExternalOptimizePort`)
at the apps/api composition root and injects it onto
``container.gomaster_external_optimize`` so the ``interfaces.http.routes.
gomaster_optimize`` routes consume it by duck-typing (interfaces-stays-thin).

Only built when the ``gomaster`` query-service is configured with
``gomaster_mode`` in ("external", "both") AND ``settings.is_internal`` — this is
the config switch that selects the external link (vs the conversational agent
link). Returns ``None`` otherwise (routes then 404).

Auth: this deployment's external/* needs no CEFlow token, so ``token_provider``
resolves to None (empty api_key). The cloud LLM ``model`` id + ``api_key`` are
resolved per-request from the user's selected chat model id: the frontend
forwards only the model id (never a credential); this bridge resolves it to the
provider's on-wire model id + SecretStore key via the SAME path chat uses
(``_model_resolver_bridge.ModelCatalogProviderLookupBridge``). Absent a selected
model, the config default model + key apply (SecretStore first, then config).

internal-only: all imports of the excluded packages are LOCAL to the
is_internal-gated body so a stripped external tree never crashes at di import.
"""

from __future__ import annotations

from typing import Any

from qai.platform.logging import get_logger

__all__ = ["build_gomaster_external_optimize_controller"]

_log = get_logger(__name__)

_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

# Strong refs to in-flight background upload tasks so the event loop does not
# garbage-collect them mid-upload (asyncio holds only weak refs to tasks).
_BACKGROUND_TASKS: set = set()


def build_gomaster_external_optimize_controller(*, container: Any) -> Any | None:
    """Build the external-optimize adapter, or ``None`` (external edition / mode off)."""
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    try:
        from qai.platform.edition import get_query_services
        from qai.platform.edition.loader import get_cloud_provider_api_keys
        from qai.model_builder.infrastructure.gomaster_external_optimize_adapter import (
            GomasterExternalOptimizeAdapter,
        )
    except Exception:  # pragma: no cover - excluded on external
        return None

    fields = get_query_services().get("gomaster")
    if not fields:
        return None

    # Config switch: only wire the external link when selected.
    mode = str(fields.get("gomaster_mode", "external")).lower()
    if mode not in ("external", "both"):
        return None

    endpoint = fields.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return None

    jobs_path = fields.get("gomaster_external_jobs_path")
    if not isinstance(jobs_path, str) or not jobs_path:
        jobs_path = "/api/external/auto-optimize/jobs"

    verify = bool(fields.get("verify_tls", True)) and not bool(
        fields.get("insecure", False)
    )
    cloud_llm_model = fields.get("gomaster_external_cloud_llm_model")
    cloud_llm_model = cloud_llm_model if isinstance(cloud_llm_model, str) and cloud_llm_model else None

    secret_store = getattr(container, "secret_store", None)

    def _token_provider() -> str | None:
        # CEFlow Bearer — currently unused (external/* needs no auth). Honoured
        # for the future: SecretStore key "gomaster" → config cloud_providers.
        if secret_store is not None:
            try:
                if secret_store.exists(_PROVIDER_SECRET_SERVICE, "gomaster"):
                    val = secret_store.get(_PROVIDER_SECRET_SERVICE, "gomaster")
                    if val:
                        return val
            except Exception:  # noqa: BLE001
                pass
        try:
            return get_cloud_provider_api_keys().get("gomaster") or None
        except Exception:  # noqa: BLE001
            return None

    def _cloud_llm_api_key_provider() -> str | None:
        # Cloud LLM api_key (form field). SecretStore key "gomaster_cloud_llm"
        # first, then config cloud_providers "gomaster_cloud_llm". Optional
        # (server may have a default configured).
        if secret_store is not None:
            try:
                if secret_store.exists(_PROVIDER_SECRET_SERVICE, "gomaster_cloud_llm"):
                    val = secret_store.get(_PROVIDER_SECRET_SERVICE, "gomaster_cloud_llm")
                    if val:
                        return val
            except Exception:  # noqa: BLE001
                pass
        try:
            return get_cloud_provider_api_keys().get("gomaster_cloud_llm") or None
        except Exception:  # noqa: BLE001
            return None

    adapter = GomasterExternalOptimizeAdapter(
        base_url=endpoint,
        jobs_path=jobs_path,
        verify=verify,
        token_provider=_token_provider,
        cloud_llm_model=cloud_llm_model,
        cloud_llm_api_key_provider=_cloud_llm_api_key_provider,
    )

    # Per-request model resolver: maps the user's selected chat cloud model id
    # (e.g. ``provider::model-name``) to that model's on-wire model id +
    # api_key, using the SAME model_catalog provider registry + SecretStore path
    # the chat context uses (apps/api/_model_resolver_bridge.py). This lets a
    # GoMaster optimize run against whichever cloud model the user picked in the
    # chat model dropdown, with that model's own key. ``None`` when the
    # model_catalog context is not wired → the adapter falls back to the config
    # defaults (previous behaviour).
    model_lookup = None
    model_catalog = getattr(container, "model_catalog", None)
    provider_registry = getattr(model_catalog, "provider_registry", None)
    if provider_registry is not None:
        try:
            from ._model_resolver_bridge import ModelCatalogProviderLookupBridge

            model_lookup = ModelCatalogProviderLookupBridge(
                provider_registry=provider_registry,
                secret_store=secret_store,
            )
        except Exception:  # noqa: BLE001 — resolver is best-effort
            model_lookup = None

    async def _resolve_model(model_id: str | None) -> tuple[str | None, str | None]:
        """Resolve a selected chat model id → (on-wire model id, api_key).

        Returns ``(None, None)`` on empty input / miss / no resolver so the
        adapter falls back to the config-default model + key.
        """
        if not model_id or model_lookup is None:
            return None, None
        try:
            resolved = await model_lookup.lookup_for_model(model_id)
        except Exception:  # noqa: BLE001 — never fail the optimize on lookup
            return None, None
        if resolved is None:
            return None, None
        # Send the model id verbatim (A): prefer the explicit wire override
        # (api_model_id) only when the catalog declares one; else the selected
        # id itself — matching chat's on-wire behaviour.
        wire_model = getattr(resolved, "api_model_id", None) or model_id
        api_key = getattr(resolved, "api_key", None) or None
        return wire_model, api_key

    # Wrap the adapter in a controller that adds background-upload + progress
    # tracking (the backend→GoMaster hop for a large model takes ~1 min; the
    # frontend polls upload progress). Job poll/download/cancel delegate to the
    # adapter unchanged.
    try:
        from qai.model_builder.infrastructure.gomaster_upload_registry import (
            get_gomaster_upload_registry,
        )
    except Exception:  # pragma: no cover - excluded on external
        return adapter
    return _GomasterExternalOptimizeController(
        adapter=adapter,
        registry=get_gomaster_upload_registry(),
        resolve_model=_resolve_model,
    )


class _GomasterExternalOptimizeController:
    """Adapter + upload-progress registry facade (background upload support).

    Delegates job status/cancel/download to the adapter; adds
    ``start_background_upload`` (fire-and-forget create-job that reports byte
    progress to the registry) + ``get_upload_progress`` for the frontend poll.
    """

    __slots__ = ("_adapter", "_registry", "_resolve_model")

    def __init__(
        self,
        *,
        adapter: Any,
        registry: Any,
        resolve_model: Any | None = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        # ``async (model_id: str | None) -> (wire_model, api_key)``; resolves the
        # user's selected chat model id to that model's on-wire id + key. ``None``
        # ⇒ no resolution (adapter uses its config defaults).
        self._resolve_model = resolve_model

    # --- background upload + progress ---------------------------------------
    def start_background_upload(
        self,
        *,
        model_filename: str,
        model_bytes: bytes,
        benchmark_requested: bool,
        run_id: str | None = None,
        start_hint: str | None = None,
        end_hint: str | None = None,
        anchor_hint: str | None = None,
        model_id: str | None = None,
    ) -> str:
        """Kick off the create-job upload in the background; return upload_id.

        The upload runs as an asyncio task; progress + the eventual job (or
        error) land in the registry, which the frontend polls.
        """
        import asyncio
        import time as _time

        entry = self._registry.create(total=len(model_bytes))
        upload_id = entry.upload_id
        total = len(model_bytes)

        def _on_progress(sent: int) -> None:
            self._registry.set_sent(upload_id, sent)

        async def _run() -> None:
            t0 = _time.monotonic()
            _log.info(
                "gomaster.optimize.upload.start",
                extra={"upload_id": upload_id, "total_bytes": total},
            )
            try:
                # Resolve the user's selected chat model id → on-wire model +
                # key (best-effort; ``(None, None)`` falls back to the adapter's
                # config defaults).
                agent_model: str | None = None
                api_key: str | None = None
                if self._resolve_model is not None:
                    agent_model, api_key = await self._resolve_model(model_id)
                job = await self._adapter.create_job(
                    model_filename=model_filename,
                    model_bytes=model_bytes,
                    benchmark_requested=benchmark_requested,
                    run_id=run_id,
                    start_hint=start_hint,
                    end_hint=end_hint,
                    anchor_hint=anchor_hint,
                    agent_model=agent_model,
                    api_key=api_key,
                    progress_cb=_on_progress,
                )
                self._registry.mark_done(upload_id, job)
                _log.info(
                    "gomaster.optimize.upload.done",
                    extra={
                        "upload_id": upload_id,
                        "elapsed_s": round(_time.monotonic() - t0, 1),
                        "job_id": job.get("job_id"),
                        "status": job.get("status"),
                        "agent_model": agent_model or "(config default)",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - surface to the poller
                self._registry.mark_error(upload_id, str(exc))
                _log.warning(
                    "gomaster.optimize.upload.error",
                    extra={
                        "upload_id": upload_id,
                        "elapsed_s": round(_time.monotonic() - t0, 1),
                        "error": str(exc),
                    },
                )

        # Fire-and-forget; the registry + poll endpoint own the lifecycle.
        # Called from within an async route ⇒ a running loop exists. Keep a
        # reference so the task isn't GC'd mid-flight.
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return upload_id

    def get_upload_progress(self, upload_id: str) -> dict[str, Any] | None:
        return self._registry.get(upload_id)

    # --- delegate job operations to the adapter -----------------------------
    async def get_job(self, *, job_id: str) -> dict[str, Any]:
        job = await self._adapter.get_job(job_id=job_id)
        # Log the FULL upstream job record when it ends in ``failed`` so the
        # failure cause is recoverable from the local server log (the adapter
        # otherwise only logs the transport-level HTTP 200 — the failure detail
        # lives in this response body, which is never persisted elsewhere). The
        # frontend surfaces the same fields in its "详细信息" block; this is the
        # server-side counterpart for offline diagnosis. Guarded so a normal
        # poll (queued/running/succeeded) stays quiet.
        if isinstance(job, dict) and job.get("status") == "failed":
            _log.warning(
                "gomaster.optimize.job_failed",
                extra={
                    "job_id": job_id,
                    "phase": job.get("phase"),
                    "error": job.get("error"),
                    "message": job.get("message"),
                    "completed_at": job.get("completed_at"),
                    # Full body (minus any large blobs) for deep inspection.
                    "job": {
                        k: v
                        for k, v in job.items()
                        if k not in ("optimized_model", "report", "recommendation")
                    },
                },
            )
        return job

    async def cancel_job(self, *, job_id: str) -> dict[str, Any]:
        return await self._adapter.cancel_job(job_id=job_id)

    async def download_model(self, *, job_id: str) -> tuple[bytes, str, str]:
        return await self._adapter.download_model(job_id=job_id)

    async def download_report(self, *, job_id: str) -> tuple[bytes, str, str]:
        return await self._adapter.download_report(job_id=job_id)
