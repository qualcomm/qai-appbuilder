# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — app-model routes (``/models`` family).

Registers the model gallery + single-model read/delete endpoints onto the
shared router built by :mod:`.__init__`. Handler bodies are byte-for-byte
identical to the pre-split module; the thin ``_services`` /
``_validate_app_model_id`` closures redefined at the top of
:func:`register` keep the handler text unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ._dto import (
    AppModelListResponse,
    AppModelResponse,
    _model_to_dto,
    _validate_app_model_id,
)

from qai.app_builder.application.use_cases.delete_app_model import (
    DeleteAppModelUseCase,
)
from qai.app_builder.application.use_cases.get_app_model import GetAppModelUseCase
from qai.app_builder.application.use_cases.list_app_models import (
    ListAppModelsUseCase,
)
from qai.app_builder.domain.errors import AppModelNotFoundError


class DeleteAppModelResponse(BaseModel):
    """DELETE ``/api/app-builder/models/{model_id}`` response body.

    缺陷 P4: previously the route returned 204 No Content, which meant the
    caller couldn't see non-fatal warnings (e.g. AV-locked ``.bin`` files
    left on disk). The frontend then reported "deleted" while the disk
    still held stale weights — a State-Truth-First violation. We now
    return a JSON envelope carrying:

    * ``model_id`` — the id that was operated on (echo for client clarity);
    * ``mode`` — ``"full"`` when the whole model was removed;
      ``"partial"`` when only some variants were deleted (per-variant
      delete path);
    * ``deleted_variants`` / ``remaining_variants`` / ``new_default`` —
      per-variant delete detail (empty tuples for a full delete);
    * ``warnings`` — non-fatal messages from ``FileSystemPackFileCleanup``
      (e.g. "weights .bin locked by another process — retry later"). The
      frontend surfaces these as a toast so the user knows the disk state
      may differ from the DB.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(..., description="Model id that was deleted")
    mode: str = Field(
        ..., description="'full' or 'partial' (per-variant delete)"
    )
    deleted_variants: list[str] = Field(default_factory=list)
    remaining_variants: list[str] = Field(default_factory=list)
    new_default: str | None = Field(
        default=None,
        description="New default variant when a per-variant delete "
        "reassigned the default; None otherwise.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal file-cleanup warnings (e.g. AV-locked .bin).",
    )


if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def register(router: APIRouter, *, container: "Container") -> None:
    """Mount the app-model routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    # ---- app models -------------------------------------------------------

    @router.get("/models", response_model=AppModelListResponse)
    async def list_models(include_disabled: bool = True) -> AppModelListResponse:
        services = _services()
        uc: ListAppModelsUseCase = services.list_app_models_use_case
        models = await uc.execute(include_disabled=include_disabled)
        # V1 parity: augment each row with weight-presence + dependency
        # status (see ``GET /api/appbuilder/models`` in V1 ``api_routes.py``).
        # The resolver is wired in DI from the pack manifest provider /
        # pack-root probe + the dependency checker; absent on stripped-down
        # test containers ⇒ rows fall back to the default ``Ready`` status.
        resolver = getattr(services, "app_model_status_resolver", None)
        return AppModelListResponse(
            items=[
                _model_to_dto(
                    m, resolver(m) if resolver is not None else None
                )
                for m in models
            ]
        )

    @router.get("/models/{model_id}", response_model=AppModelResponse)
    async def get_model(model_id: str) -> AppModelResponse:
        vid = _validate_app_model_id(model_id)
        services = _services()
        uc: GetAppModelUseCase = services.get_app_model_use_case
        try:
            model = await uc.execute(model_id=vid)
        except AppModelNotFoundError as exc:
            # DomainError → global 422; a REST GET on a missing id wants
            # 404. Manual conversion is consistent with the DELETE route
            # below and with sibling _catalog.py:277 / _runs.py:504.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        resolver = getattr(services, "app_model_status_resolver", None)
        return _model_to_dto(
            model, resolver(model) if resolver is not None else None
        )

    @router.delete(
        "/models/{model_id}",
        status_code=status.HTTP_200_OK,
        response_model=DeleteAppModelResponse,
    )
    async def delete_model(
        model_id: str,
        variantIds: str | None = None,
        deleteFiles: bool = True,
    ) -> DeleteAppModelResponse:
        """Delete an imported App Builder model.

        HTTP mapping (缺陷 P1 / P4):
        * 200 + JSON envelope with ``deleted`` / ``warnings`` on success.
          Prior version returned 204 No Content, which meant the caller
          couldn't see non-fatal warnings (e.g. AV-locked ``.bin`` files
          left on disk) — the UI reported "deleted" while the disk still
          held stale weights (State-Truth-First 铁律 3 violation).
        * 404 if the model_id is unknown (previously mapped to 422 by the
          global DomainError handler — REST-shaped 404 is what clients
          expect on DELETE of an absent resource).
        * 403 if the target is a built-in model (``user_imported=False`` —
          protection enforced by the use case).

        ``variantIds=fp16,int8`` (V1 parity) → per-variant delete; otherwise
        a full delete. ``deleteFiles`` defaults to True (V1 default) so the
        on-disk pack dir + staged weights are removed, not just the DB row.
        """
        vid = _validate_app_model_id(model_id)
        variant_tuple: tuple[str, ...] = ()
        if variantIds:
            variant_tuple = tuple(
                v.strip() for v in variantIds.split(",") if v.strip()
            )
        uc: DeleteAppModelUseCase = _services().delete_app_model_use_case
        try:
            result = await uc.execute(
                model_id=vid,
                variant_ids=variant_tuple,
                delete_files=deleteFiles,
            )
        except AppModelNotFoundError as exc:
            # DomainError → global 422; a REST DELETE on a missing id wants
            # 404. Manual conversion (same pattern as _catalog.py:277 and
            # _runs.py:504) keeps the constructor / base-class contract
            # untouched while giving clients the correct status code.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # NOTE: ForbiddenError is intentionally NOT caught here — the
        # global error handler routes it to HTTP 403 with the unified
        # error envelope (``{"type": "ForbiddenError", "code":
        # "app_builder.app_model_builtin_protected", ...}``) which is
        # what existing clients + the frontend already consume for
        # "built-in model protected" (see test_delete_builtin_model_forbidden).
        return DeleteAppModelResponse(
            model_id=vid.value,
            mode=result.mode,
            deleted_variants=list(result.deleted_variants),
            remaining_variants=list(result.remaining_variants),
            new_default=result.new_default,
            warnings=list(result.warnings),
        )
