# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — catalogue / registry / status routes.

The read-mostly registry surface: worker status, taxonomy (flat + full
static tree), dependency status, blob cache status/clear, pack manifest
and per-model schema.

Handler bodies are byte-for-byte identical to the pre-split module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ._dto import (
    CacheClearResponse,
    CacheStatusResponse,
    DepsProgressResponse,
    DepsStatusResponse,
    LoadedModelDTO,
    ModelSchemaResponse,
    PackDepProgressResponse,
    PackManifestResponse,
    TaxonomyNodeResponse,
    TaxonomyTreeGroupResponse,
    TaxonomyTreeResponse,
    TaxonomyTreeTaskResponse,
    WorkerStatusResponse,
)

from qai.app_builder.application.use_cases.deferred_routes import (
    ManifestNotAvailableError,
)
from qai.app_builder.application.use_cases.get_worker_status import (
    GetWorkerStatusUseCase,
)
from qai.app_builder.domain.value_objects import AppModelId

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def register(router: APIRouter, *, container: "Container") -> None:
    """Mount the catalogue / status routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    # ---- worker status (PR-045 / issue d, expanded by PR-301) -----------

    @router.get("/worker/status", response_model=WorkerStatusResponse)
    async def get_worker_status() -> WorkerStatusResponse:
        uc: GetWorkerStatusUseCase = _services().get_worker_status_use_case
        snapshot = await uc.execute()
        return WorkerStatusResponse(
            total_workers=snapshot.total_workers,
            busy_workers=snapshot.busy_workers,
            queued_runs=snapshot.queued_runs,
            alive=snapshot.alive,
            state=snapshot.state,
            active_model_id=snapshot.active_model_id,
            multimodel=snapshot.multimodel,
            loaded_models=[
                LoadedModelDTO(
                    model_id=info.model_id,
                    variant_id=info.variant_id,
                    last_used_at=info.last_used_at,
                    age_seconds=info.age_seconds,
                    state=info.state,
                )
                for info in snapshot.loaded_models
            ],
        )

    # ============================================================
    # PR-304 — 16 deferred routes
    # ============================================================

    # ---- 1. taxonomy ---------------------------------------------------
    @router.get(
        "/taxonomy",
        response_model=list[TaxonomyNodeResponse],
    )
    async def get_taxonomy() -> list[TaxonomyNodeResponse]:
        uc = _services().get_taxonomy_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="taxonomy use case not wired")
        nodes = await uc.execute()
        return [
            TaxonomyNodeResponse(path=list(n.path), model_count=n.model_count)
            for n in nodes
        ]

    # ---- 1b. taxonomy/tree (full static tree, V1 parity) --------------
    @router.get(
        "/taxonomy/tree",
        response_model=TaxonomyTreeResponse,
    )
    async def get_taxonomy_tree() -> TaxonomyTreeResponse:
        uc = _services().get_taxonomy_tree_use_case
        if uc is None:
            raise HTTPException(
                status_code=503, detail="taxonomy tree use case not wired"
            )
        tree = await uc.execute()
        return TaxonomyTreeResponse(
            version=tree.version,
            groups=[
                TaxonomyTreeGroupResponse(
                    id=g.id,
                    label=g.label,
                    icon=g.icon,
                    tasks=[
                        TaxonomyTreeTaskResponse(
                            id=t.id,
                            label=t.label,
                            description=t.description,
                            io=list(t.io),
                            model_count=t.model_count,
                        )
                        for t in g.tasks
                    ],
                )
                for g in tree.groups
            ],
        )

    # ---- 2. deps-status -----------------------------------------------
    @router.get("/deps-status", response_model=DepsStatusResponse)
    async def get_deps_status() -> DepsStatusResponse:
        uc = _services().get_deps_status_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="deps-status use case not wired")
        s = await uc.execute()
        return DepsStatusResponse(
            qairt_env_present=s.qairt_env_present,
            pack_root_present=s.pack_root_present,
            shared_dir_present=s.shared_dir_present,
            sticky_worker_alive=s.sticky_worker_alive,
            registered_pack_count=s.registered_pack_count,
        )

    # ---- 2b. deps-status/packs (V1 逐 pack 进度 parity) ----------------
    @router.get("/deps-status/packs", response_model=DepsProgressResponse)
    async def get_deps_status_packs() -> DepsProgressResponse:
        """Per-pack dependency-install progress (V1 ``deps-status`` shape).

        Restores the逐 pack 进度 the V1 gallery polled every 5s
        (``backend/app_builder/api_routes.py:835`` →
        ``frontend/js/composables/useAppBuilderRegistry.js:269-342``).
        The companion ``GET /deps-status`` above keeps its environment-
        overview shape unchanged (AGENTS.md §3.1 — additive only).

        V1 triggered the background probe on ``GET /models``; here the
        first poll proactively schedules ``trigger_background_check`` over
        the wired pack descriptors so a freshly-dropped Pack starts
        installing without waiting for a run (State-Truth-First — the
        rows always reflect the checker's real probe / install outcome).
        """
        services = _services()
        checker = getattr(services, "dep_checker", None)
        if checker is None:
            # Checker disabled by ``Settings.app_builder.dep_checker_enabled``;
            # report an empty, not-checking snapshot (front-end falls back to
            # the static install-status badge).
            return DepsProgressResponse(checking=False, packs={})

        # V1 parity: proactively kick the background probe so packs start
        # installing on poll, not only on first run. ``trigger_background_check``
        # is idempotent (no-op while a check is in flight or within the
        # cool-down window).
        descriptors_provider = getattr(
            services, "pack_dep_descriptors", None
        )
        if descriptors_provider is not None:
            try:
                descriptors = descriptors_provider()
            except Exception:  # noqa: BLE001 — descriptor build must not 500 the poll
                descriptors = None
            if descriptors:
                try:
                    checker.trigger_background_check(list(descriptors))
                except Exception:  # noqa: BLE001 — probe scheduling is best-effort
                    pass

        progress = checker.get_progress()
        return DepsProgressResponse(
            checking=bool(progress.get("checking", False)),
            packs={
                pack_id: PackDepProgressResponse(
                    satisfied=bool(row.get("satisfied", True)),
                    missing=list(row.get("missing", [])),
                    installing=bool(row.get("installing", False)),
                    errorKind=row.get("errorKind"),
                    errorHint=row.get("errorHint"),
                    errorRaw=row.get("errorRaw"),
                )
                for pack_id, row in dict(progress.get("packs", {})).items()
            },
        )

    # ---- 9. cache/status ----------------------------------------------
    @router.get("/cache/status", response_model=CacheStatusResponse)
    async def get_cache_status() -> CacheStatusResponse:
        uc = _services().get_cache_status_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="cache use case not wired")
        s = await uc.execute()
        return CacheStatusResponse(
            blob_count=s.blob_count,
            total_bytes=s.total_bytes,
            blob_dir=s.blob_dir,
        )

    # ---- 10. cache (clear) --------------------------------------------
    @router.delete("/cache", response_model=CacheClearResponse)
    async def clear_cache() -> CacheClearResponse:
        uc = _services().clear_cache_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="cache use case not wired")
        deleted = await uc.execute()
        return CacheClearResponse(deleted_files=deleted)

    # ---- 16. models/{model_id}/manifest --------------------------------
    @router.get(
        "/models/{model_id}/manifest",
        response_model=PackManifestResponse,
    )
    async def get_pack_manifest(model_id: str) -> PackManifestResponse:
        from qai.app_builder.domain.errors import (
            AppModelNotFoundError as _AMNF,
        )

        uc = _services().get_pack_manifest_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="manifest use case not wired")
        try:
            manifest = await uc.execute(AppModelId(value=model_id))
        except _AMNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ManifestNotAvailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PackManifestResponse(
            schema_version=manifest.schema_version,
            model_id=manifest.model_id,
            display_name=manifest.display_name,
            version=manifest.version,
            vendor=manifest.vendor,
            description=manifest.description,
            long_description=manifest.long_description,
            tags=list(manifest.tags),
            runtime={
                "backend": manifest.runtime.backend,
                "delegate": manifest.runtime.delegate,
                "quantization": manifest.runtime.quantization,
                "modelSizeMB": manifest.runtime.model_size_mb,
                "contextBins": list(manifest.runtime.context_bins),
                "supportedDevices": list(manifest.runtime.supported_devices),
            },
            runner={
                "type": manifest.runner.type,
                "script": manifest.runner.script,
                "venv": manifest.runner.venv,
                "requirements": manifest.runner.requirements,
                "timeoutMs": manifest.runner.timeout_ms,
            },
            capabilities={
                "streaming": manifest.capabilities.streaming,
                "batch": manifest.capabilities.batch,
                "benchmark": manifest.capabilities.benchmark,
                "cancel": manifest.capabilities.cancel,
            },
            input_schema=(
                {
                    "kind": manifest.input_schema.kind,
                    "constraints": manifest.input_schema.constraints_dict,
                }
                if manifest.input_schema is not None
                else None
            ),
            output_schema=(
                {
                    "kind": manifest.output_schema.kind,
                    "constraints": manifest.output_schema.constraints_dict,
                    "jsonSchema": manifest.output_schema.json_schema_dict,
                }
                if manifest.output_schema is not None
                else None
            ),
            params=[
                {
                    "name": p.name,
                    "label": p.label,
                    "type": p.type,
                    "default": p.default,
                    # V1 ParamSchema parity: range hints / select options /
                    # advanced grouping so the UI renders sliders / dropdowns.
                    **({"min": p.min} if p.min is not None else {}),
                    **({"max": p.max} if p.max is not None else {}),
                    **({"step": p.step} if p.step is not None else {}),
                    **({"options": list(p.options)} if p.options is not None else {}),
                    **({"advanced": True} if p.advanced else {}),
                }
                for p in manifest.params
            ],
            metrics={
                "latencyMs": manifest.metrics.latency_ms,
                "memoryMB": manifest.metrics.memory_mb,
            },
            assets={
                "weightsUrl": manifest.assets.weights_url,
                "checksum": manifest.assets.checksum,
                "sizeBytes": manifest.assets.size_bytes,
                "installPath": manifest.assets.install_path,
            },
            skill={
                "enabled": manifest.skill.enabled,
                "file": manifest.skill.file,
            },
            variants=[
                {
                    "id": v.id,
                    "label": v.label,
                    "longLabel": v.long_label,
                    "default": v.default,
                    "runtime": {
                        "backend": v.runtime.backend,
                        "delegate": v.runtime.delegate,
                        "quantization": v.runtime.quantization,
                        "modelSizeMB": v.runtime.model_size_mb,
                        "contextBins": list(v.runtime.context_bins),
                        "supportedDevices": list(v.runtime.supported_devices),
                    },
                    "assets": {
                        "weightsUrl": v.assets.weights_url,
                        "checksum": v.assets.checksum,
                        "sizeBytes": v.assets.size_bytes,
                        "installPath": v.assets.install_path,
                    },
                    "metrics": {
                        "latencyMs": v.metrics.latency_ms,
                        "memoryMB": v.metrics.memory_mb,
                    },
                    "createdAt": v.created_at,
                }
                for v in manifest.variants
            ],
            taxonomy_tags=list(manifest.taxonomy_tags),
            examples=[
                {
                    "name": ex.name,
                    "license": ex.license,
                    **({"inputs": ex.inputs} if ex.inputs else {}),
                    **({"paramsOverride": ex.params_override} if ex.params_override else {}),
                }
                for ex in manifest.examples
            ],
            send_to_chat_prompt=dict(manifest.send_to_chat_prompt),
        )

    # ============================================================
    # PR-305 — SKILL.md + Schema-driven UI + appbuilder_run
    # ============================================================

    # ---- 18. models/{id}/schema ---------------------------------------
    @router.get(
        "/models/{model_id}/schema",
        response_model=ModelSchemaResponse,
    )
    async def get_model_schema(model_id: str) -> ModelSchemaResponse:
        from qai.app_builder.domain.errors import (
            AppModelNotFoundError as _AMNF,
        )

        uc = _services().get_model_schema_use_case
        if uc is None:
            raise HTTPException(status_code=503, detail="schema use case not wired")
        try:
            schema = await uc.execute(AppModelId(value=model_id))
        except _AMNF as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ModelSchemaResponse(
            model_id=schema.model_id,
            title=schema.title,
            input_schema=schema.input_schema,
            output_schema=schema.output_schema,
            variants=list(schema.variants),
        )
