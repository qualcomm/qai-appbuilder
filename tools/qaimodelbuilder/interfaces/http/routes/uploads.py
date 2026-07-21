# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Uploads HTTP routes (PR-605).

Platform-level file upload endpoints under ``/api/uploads``. These
routes serve the cross-BC upload abstraction provided by
:mod:`qai.platform.uploads`. Any bounded context that needs file
upload capabilities (chat images, app_builder models, etc.) can
leverage these routes.

Route summary
-------------
* ``POST /api/uploads/image``    — upload an image file
* ``POST /api/uploads/model``    — upload a model file
* ``POST /api/uploads/code``     — upload code file(s)
* ``POST /api/uploads/dataset``  — upload a dataset file
* ``POST /api/uploads/audio``    — upload an audio file
* ``POST /api/uploads/voice``    — upload a voice file
* ``GET  /api/uploads``          — list recent uploads
* ``DELETE /api/uploads/{upload_id}`` — delete an upload
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from qai.platform.uploads import (
    DatasetExtractionError,
    UnsupportedExtensionError,
    UploadCategory,
    UploadCodeFileUseCase,
    UploadDatasetUseCase,
    UploadFileUseCase,
    UploadStorePort,
    UploadTooLargeError,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Response DTOs
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    """Response shape for a single upload operation."""

    id: str
    path: str
    size: int


class DatasetFileEntry(BaseModel):
    """One stored file produced by a dataset upload (V1 extracted entry)."""

    id: str
    path: str
    size: int
    filename: str


class DatasetUploadResponse(UploadResponse):
    """Response for ``POST /api/uploads/dataset`` (archive auto-extraction).

    Backward-compatible superset of :class:`UploadResponse`: ``id`` /
    ``path`` / ``size`` describe the *first* stored file (an archive's
    first member, or the single plain file), so existing callers keep
    working. The appended ``count`` + ``files`` surface every file an
    extracted archive yielded (V1 parity: "uploaded & extracted N
    files"). All §3.1-additive (tail-only) — no existing field changed.
    """

    count: int = 1
    files: list[DatasetFileEntry] = []
    #: Server-side directory holding the uploaded dataset blobs (V1 parity:
    #: the backend, which owns the storage layout, reports the dataset dir
    #: directly so the client doesn't fragilely string-slice a file path).
    #: Tail-appended (§3.1-additive).
    dir: str = ""


class CodeUploadResponse(BaseModel):
    """Response shape for the legacy ``POST /api/upload/code`` route.

    Matches the V1 contract (``backend/main.py`` L6096-6100) so the
    chat ``code`` mode sub-toolbar can write ``tool_params.file_path``
    from ``path`` and show the original ``filename``.
    """

    path: str
    filename: str
    size: int


class UploadListResponse(BaseModel):
    """Response shape for listing uploads."""

    uploads: list[dict[str, Any]]


class DeleteResponse(BaseModel):
    """Response shape for delete operation."""

    status: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, store: UploadStorePort) -> APIRouter:
    """Build the uploads router bound to the given upload store.

    Parameters:
        store: the :class:`UploadStorePort` implementation to use.
    """

    router = APIRouter(prefix="/api/uploads", tags=["uploads"])

    # Upload policy (per-category size ceilings, code-file extension
    # allowlist, legacy 10 MB code cap) lives in the platform uploads
    # use cases / policy module — the route only orchestrates
    # request → use case → DTO and translates policy errors to HTTP.
    upload_file_use_case = UploadFileUseCase(store=store)
    upload_code_file_use_case = UploadCodeFileUseCase(store=store)
    upload_dataset_use_case = UploadDatasetUseCase(store=store)

    # Legacy V1-parity routes live outside the ``/api/uploads`` prefix
    # (e.g. ``/api/upload/code``). They are registered on a sibling
    # un-prefixed router and combined with ``router`` under a prefix-less
    # outer router so both surfaces are returned from one factory.
    legacy_router = APIRouter(tags=["uploads"])

    # ------------------------------------------------------------------
    # Upload helpers
    # ------------------------------------------------------------------

    async def _handle_upload(
        category: UploadCategory,
        file: UploadFile,
        conv_id: str | None = None,
    ) -> UploadResponse:
        """Common upload handler for all categories."""
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file has no filename.",
            )
        content = await file.read()
        try:
            record = await upload_file_use_case.execute(
                category=category,
                filename=file.filename,
                content=content,
                conv_id=conv_id,
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=exc.detail) from exc
        return UploadResponse(
            id=record.id,
            path=str(record.path),
            size=record.size_bytes,
        )

    # ------------------------------------------------------------------
    # POST routes (one per category)
    # ------------------------------------------------------------------

    @router.post(
        "/image",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_image(
        file: UploadFile = File(..., description="Image file"),
    ) -> UploadResponse:
        return await _handle_upload(UploadCategory.IMAGE, file)

    @router.post(
        "/model",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_model(
        file: UploadFile = File(..., description="Model file"),
        conv_id: str | None = Form(None, description="Conversation ID for per-session isolation (V1 parity)"),
    ) -> UploadResponse:
        return await _handle_upload(UploadCategory.MODEL, file, conv_id=conv_id)

    @router.post(
        "/code",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_code(
        file: UploadFile = File(..., description="Code file"),
    ) -> UploadResponse:
        return await _handle_upload(UploadCategory.CODE, file)

    @router.post(
        "/dataset",
        response_model=DatasetUploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_dataset(
        file: UploadFile = File(..., description="Dataset file (or .zip/.tar archive — auto-extracted)"),
        conv_id: str | None = Form(None, description="Conversation ID for per-session isolation (V1 parity)"),
    ) -> DatasetUploadResponse:
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file has no filename.",
            )
        content = await file.read()
        try:
            records = await upload_dataset_use_case.execute(
                filename=file.filename,
                content=content,
                conv_id=conv_id,
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=exc.detail) from exc
        except DatasetExtractionError as exc:
            # V1 parity (main.py L6173/L6184): illegal archive path /
            # corrupt archive → HTTP 400.
            raise HTTPException(status_code=400, detail=exc.detail) from exc
        if not records:
            # Empty archive — nothing stored. Treat as a 400 so the user
            # is not misled into thinking files were ingested.
            raise HTTPException(
                status_code=400,
                detail="Dataset archive contained no files.",
            )
        first = records[0]
        return DatasetUploadResponse(
            id=first.id,
            path=str(first.path),
            size=first.size_bytes,
            count=len(records),
            dir=str(Path(first.path).parent),
            files=[
                DatasetFileEntry(
                    id=r.id,
                    path=str(r.path),
                    size=r.size_bytes,
                    filename=r.filename,
                )
                for r in records
            ],
        )

    @router.post(
        "/audio",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_audio(
        file: UploadFile = File(..., description="Audio file"),
    ) -> UploadResponse:
        return await _handle_upload(UploadCategory.AUDIO, file)

    @router.post(
        "/voice",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_voice(
        file: UploadFile = File(..., description="Voice file"),
    ) -> UploadResponse:
        return await _handle_upload(UploadCategory.VOICE, file)

    # ------------------------------------------------------------------
    # POST /api/upload/code — legacy V1-parity code-file upload.
    #
    # Distinct from the generic ``/api/uploads/code`` route above:
    #   * lives at the singular ``/api/upload/code`` path the chat
    #     ``code`` mode sub-toolbar posts to (V1 parity);
    #   * enforces the legacy extension allowlist + 10 MB cap;
    #   * returns ``{path, filename, size}`` (NOT ``{id, path, size}``)
    #     so the frontend can write ``tool_params.file_path`` and show
    #     the original filename.
    # ------------------------------------------------------------------

    @legacy_router.post(
        "/api/upload/code",
        response_model=CodeUploadResponse,
        status_code=status.HTTP_200_OK,
    )
    async def upload_code_file(
        file: UploadFile = File(..., description="Code file"),
    ) -> CodeUploadResponse:
        filename = file.filename or ""
        content = await file.read()
        try:
            record = await upload_code_file_use_case.execute(
                filename=filename,
                content=content,
            )
        except UnsupportedExtensionError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exc.detail,
            ) from exc
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=exc.detail) from exc
        return CodeUploadResponse(
            path=str(record.path),
            filename=filename or record.filename,
            size=record.size_bytes,
        )

    # ------------------------------------------------------------------
    # GET /api/uploads — list recent
    # ------------------------------------------------------------------

    @router.get("", response_model=UploadListResponse)
    async def list_uploads(
        conv_id: str | None = Query(None, description="Filter by conversation ID (V1 parity)"),
    ) -> UploadListResponse:
        records = await store.list_recent(limit=50, conv_id=conv_id)
        return UploadListResponse(
            uploads=[
                {
                    "id": r.id,
                    "category": r.category.value,
                    "filename": r.filename,
                    "size_bytes": r.size_bytes,
                    "path": str(r.path),
                    "created_at": r.created_at.isoformat(),
                    "conv_id": r.conv_id,
                }
                for r in records
            ]
        )

    # ------------------------------------------------------------------
    # DELETE /api/uploads/{upload_id}
    # ------------------------------------------------------------------

    @router.delete("/{upload_id}", response_model=DeleteResponse)
    async def delete_upload(upload_id: str) -> DeleteResponse:
        deleted = await store.delete(upload_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Upload '{upload_id}' not found.",
            )
        return DeleteResponse(status="deleted")

    # Combine the prefixed uploads surface with the legacy un-prefixed
    # routes under a single prefix-less outer router so callers mount
    # one router and both ``/api/uploads/*`` and ``/api/upload/code``
    # resolve correctly.
    outer = APIRouter()
    outer.include_router(router)
    outer.include_router(legacy_router)
    return outer
