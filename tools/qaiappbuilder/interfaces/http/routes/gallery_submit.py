# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Gallery submission HTTP routes.

Endpoints for scanning a model work directory and submitting packaged
artifacts to the external Model Gallery dashboard.

Route summary
-------------
* ``GET  /api/gallery/scan``   — scan workdir for submittable artifacts
* ``POST /api/gallery/submit`` — package and forward to gallery dashboard
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx
from fastapi import APIRouter, Query
from qai.platform.config.settings import LOOPBACK_HOSTS, PUBLIC_BIND_SENTINELS
from qai.platform.errors import (
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from pydantic import BaseModel, EmailStr

if TYPE_CHECKING:  # pragma: no cover
    from dependency_injector.containers import DeclarativeContainer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GALLERY_UPLOAD_URL = "http://modelgallery.qualcomm.com:3000/api/uploads/submit"

_SCANNABLE_EXTENSIONS: set[str] = {".dlc", ".bin", ".md", ".py"}

_QUANT_PATTERN = re.compile(
    r"(fp16|fp32|w8a16|w4a16|int8)", re.IGNORECASE
)

_QNN_VERSION_PATTERN = re.compile(
    r"(?:qairt|qnn)[_\-\s]*(?:sdk)?[_\-\s]*v?([\d]+\.[\d]+(?:\.[\d]+)?)",
    re.IGNORECASE,
)

#: In-process registry of background gallery uploads, keyed by our own
#: ``upload_id`` (a uuid, distinct from the dashboard's upload_id). Each entry
#: is a mutable dict the background task updates and the progress route reads.
#: This is intentionally process-local (single-worker desktop app); it mirrors
#: the ``gomaster_optimize`` background-upload + poll pattern.
_UPLOADS: dict[str, dict[str, Any]] = {}

#: Strong refs to in-flight upload tasks so the event loop does not GC them
#: mid-upload (asyncio holds only weak refs to tasks).
_UPLOAD_TASKS: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


class ScannedFileEntry(BaseModel):
    """One file discovered during a workdir scan."""

    path: str
    filename: str
    size: int
    selected: bool
    type: str
    #: Path of the file relative to the scanned workdir, POSIX-style
    #: (e.g. ``sub/dir/model.dlc``). Equals ``filename`` for top-level
    #: files. Lets the UI disambiguate same-named files in different
    #: subdirectories.
    rel_path: str = ""

class GalleryScanResponse(BaseModel):
    """Response shape for ``GET /api/gallery/scan``."""

    model_config = {"protected_namespaces": ()}

    model_name: str
    category: str
    qnn_version: Optional[str] = None
    quant_method: Optional[str] = None
    files: list[ScannedFileEntry]


class GallerySubmitRequest(BaseModel):
    """Request body for ``POST /api/gallery/submit``."""

    model_config = {"protected_namespaces": ()}

    submitter: str
    email: str
    model_name: str
    model_category: str  # genai | non_genai
    model_type: Optional[str] = None  # LLM/VLM/LVM/Omni/Embedding/MoE
    customer: Optional[str] = None
    qnn_version: Optional[str] = None
    quant_method: Optional[str] = None
    scenario: Optional[str] = None
    notebook_url: Optional[str] = None
    description: Optional[str] = None
    performance: Optional[str] = None
    file_paths: list[str]


class GallerySubmitResponse(BaseModel):
    """Response shape for ``POST /api/gallery/submit``."""

    upload_id: str
    status_url: str
    success: bool
    message: str



class GallerySubmitAccepted(BaseModel):
    """Immediate response for ``POST /api/gallery/submit`` — upload runs in
    the background; poll ``GET /api/gallery/submit-progress/{upload_id}``."""

    upload_id: str
    status: str = "uploading"


class GallerySubmitProgress(BaseModel):
    """Snapshot for ``GET /api/gallery/submit-progress/{upload_id}``.

    Progress is **phase-based and indeterminate**, not a byte percentage.
    Measured reality (see PR notes): the file bytes flush to the OS/network
    buffer near-instantly; the multi-second wait is the upstream dashboard
    *receiving + processing* the batch, which exposes no progress signal. So
    a byte-percent bar would lie (jump to 100% then freeze). Instead:

    * ``phase`` — ``packaging`` (reading files from disk + encoding, which is
      genuinely per-file and slow for large DLCs), ``uploading`` (POST in
      flight — indeterminate), ``done``, or ``error``.
    * ``current_file`` — the file being read during ``packaging``.
    * ``files_done`` / ``files_total`` — packaging progress across files.
    """

    status: str  # packaging | uploading | done | error
    phase: str = "packaging"
    current_file: Optional[str] = None
    files_done: int = 0
    files_total: int = 0
    result: Optional[GallerySubmitResponse] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class _OpenFileRequest(BaseModel):
    """Request body for ``POST /api/gallery/open-file``."""

    path: str


class _DeleteFileRequest(BaseModel):
    """Request body for ``POST /api/gallery/delete-file``."""

    path: str


class _GenerateDescriptionRequest(BaseModel):
    """Request body for ``POST /api/gallery/generate-description``."""

    model_config = {"protected_namespaces": ()}

    workdir: str
    #: Optional chat model id the user has selected; when omitted the
    #: server's configured default cloud model is used (parity with
    #: ``POST /api/prompt/enhance``).
    model_id: Optional[str] = None


class _GenerateDescriptionResponse(BaseModel):
    """Response shape for ``POST /api/gallery/generate-description``."""

    description: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_file_type(ext: str) -> str:
    """Map file extension to a type label (extension without dot)."""
    return ext.lstrip(".") if ext else "other"


def _infer_quant_method(filenames: list[str]) -> Optional[str]:
    """Attempt to infer quantization method from DLC filenames."""
    for name in filenames:
        match = _QUANT_PATTERN.search(name)
        if match:
            return match.group(1).lower()
    return None


def _infer_qnn_version(workdir: Path) -> Optional[str]:
    """Scan .py and .log files for a QAIRT/QNN SDK version reference."""
    text_globs = ["*.py", "*.log", "*.sh", "*.bat", "*.txt"]
    for pattern in text_globs:
        for fpath in workdir.rglob(pattern):
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
                match = _QNN_VERSION_PATTERN.search(content)
                if match:
                    return match.group(1)
            except OSError:
                continue
    return None


def _collect_files(workdir: Path) -> list[ScannedFileEntry]:
    """Recursively collect scannable files from *workdir*."""
    entries: list[ScannedFileEntry] = []
    for fpath in sorted(workdir.rglob("*")):
        if not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext not in _SCANNABLE_EXTENSIONS:
            continue
        try:
            rel = fpath.relative_to(workdir).as_posix()
        except ValueError:
            rel = fpath.name
        entries.append(
            ScannedFileEntry(
                path=str(fpath),
                filename=fpath.name,
                size=fpath.stat().st_size,
                selected=True,
                type=_infer_file_type(ext),
                rel_path=rel,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Description generation (direct cloud-model call)
# ---------------------------------------------------------------------------

#: Doc filenames preferred as the description source, highest priority first.
_DESC_DOC_PRIORITY: tuple[str, ...] = ("model_card.md", "readme.md")

#: Cap the doc text fed to the model so a huge README can't blow the context.
_DESC_DOC_MAX_CHARS: int = 8000


def _is_loopback(base_url: str) -> bool:
    """True when *base_url*'s host is a loopback / local address.

    Uses the allow-listed sets in :mod:`qai.platform.config.settings` rather
    than its own literals: ``LOOPBACK_HOSTS`` covers the loopback aliases
    (``127.0.0.1`` / ``::1`` / ``localhost``) and ``PUBLIC_BIND_SENTINELS``
    covers the "all interfaces" forms (``0.0.0.0`` / ``::``), which are equally
    unreachable for anyone but this machine.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and (host in LOOPBACK_HOSTS or host in PUBLIC_BIND_SENTINELS)

_DESC_SYSTEM_PROMPT: str = (
    "You are a technical writer for a model gallery. Given a model's "
    "documentation, write ONE concise description (2-4 sentences) summarizing "
    "the model's purpose, conversion approach, and key characteristics. "
    "Reply with ONLY the description prose in the SAME language as the "
    "documentation — no headings, no lists, no preamble, no markdown."
)


def _find_description_doc(workdir: Path) -> Optional[Path]:
    """Pick the best documentation file to summarise for the description.

    Priority: ``MODEL_CARD.md`` → ``README.md`` → any other ``*.md``
    (shallowest, then alphabetical). Returns ``None`` when the workdir has
    no markdown at all.
    """
    md_files = [p for p in workdir.rglob("*.md") if p.is_file()]
    if not md_files:
        return None
    for preferred in _DESC_DOC_PRIORITY:
        for p in md_files:
            if p.name.lower() == preferred:
                return p
    # Fall back to the shallowest, then alphabetically-first .md.
    md_files.sort(key=lambda p: (len(p.relative_to(workdir).parts), p.name.lower()))
    return md_files[0]


async def _generate_description_via_cloud(
    *,
    container: DeclarativeContainer,
    doc_text: str,
    model_id: Optional[str],
) -> str:
    """Call the user's cloud model to summarise *doc_text* into a description.

    Resolves the endpoint through the SAME provider-aware resolver the chat
    hot path uses (``container.chat.model_resolver``), so the request routes
    to whichever cloud model the user selected (or the configured default).
    Refuses local / loopback endpoints — description generation is a cloud-only
    feature (parity with title generation), raising 400 so the UI can tell the
    user to pick a cloud model.
    """
    chat = getattr(container, "chat", None)
    resolver = getattr(chat, "model_resolver", None)
    if resolver is None:
        raise ExternalServiceError(
            "gallery.model_resolver_unavailable",
            "No model resolver available; cloud description generation is not configured.",
            service="gallery_description",
        )
    try:
        resolved = await resolver.resolve(model_id or None)
    except Exception as exc:  # noqa: BLE001
        raise ExternalServiceError(
            "gallery.model_resolve_failed",
            f"Failed to resolve a model endpoint: {exc}",
            service="gallery_description",
            cause=exc,
        ) from exc

    base_url = (resolved.base_url or "").rstrip("/")
    if resolved.is_local or not base_url or _is_loopback(base_url):
        raise ValidationError(
            "gallery.description_requires_cloud",
            "Description generation requires a cloud model. Select a cloud model and try again.",
        )

    wire_model = resolved.api_model_id or resolved.model_id or (model_id or "qai-default")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if resolved.api_key:
        headers["Authorization"] = f"Bearer {resolved.api_key}"
    payload = {
        "model": wire_model,
        "messages": [
            {"role": "system", "content": _DESC_SYSTEM_PROMPT},
            {"role": "user", "content": doc_text[:_DESC_DOC_MAX_CHARS]},
        ],
        "stream": False,
        "max_tokens": 400,
        "temperature": 0.4,
    }
    ssl_verify = bool(getattr(getattr(container, "settings", None), "ssl_verify", True))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), verify=ssl_verify) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise ExternalServiceError(
            "gallery.cloud_model_http_error",
            f"Cloud model returned {exc.response.status_code}: {exc.response.text[:300]}",
            service="gallery_description",
            status=exc.response.status_code,
            cause=exc,
        ) from exc
    except httpx.RequestError as exc:
        raise ExternalServiceError(
            "gallery.cloud_model_unreachable",
            f"Failed to reach cloud model: {exc}",
            service="gallery_description",
            cause=exc,
        ) from exc

    choices = data.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    description = content.strip() if isinstance(content, str) else ""
    if not description:
        raise ExternalServiceError(
            "gallery.cloud_model_empty",
            "Cloud model returned an empty description.",
            service="gallery_description",
        )
    return description


async def _do_background_upload(
    *,
    upload_id: str,
    form_data: dict[str, str],
    other_files: list[str],
    py_files: list[str],
    zip_scripts: bool,
    ssl_verify: bool,
) -> None:
    """Package files (per-file progress) then upload to the gallery dashboard.

    Updates ``_UPLOADS[upload_id]`` through the ``packaging`` phase (reading
    each file from disk — genuinely slow + per-file for large DLCs) and the
    ``uploading`` phase (POST in flight; indeterminate because the upstream
    exposes no receive/process progress). Never raises: all failures land in
    the progress entry so the polling frontend surfaces them.
    """
    entry = _UPLOADS.get(upload_id)
    if entry is None:  # pragma: no cover — registered right before spawn
        return
    try:
        multipart_files: list[tuple[str, tuple[str, bytes, str]]] = []
        done = 0

        # --- packaging: read non-py files individually ---
        for fpath in other_files:
            file_path = Path(fpath)
            entry["current_file"] = file_path.name
            # Yield control so a poll between reads sees the current file.
            await asyncio.sleep(0)
            data = await asyncio.to_thread(file_path.read_bytes)
            multipart_files.append(
                ("files", (file_path.name, data, _guess_mime(file_path.suffix.lower())))
            )
            done += 1
            entry["files_done"] = done

        # --- packaging: python scripts (≤5 individual, >5 zipped) ---
        if py_files and not zip_scripts:
            for fpath in py_files:
                file_path = Path(fpath)
                entry["current_file"] = file_path.name
                await asyncio.sleep(0)
                data = await asyncio.to_thread(file_path.read_bytes)
                multipart_files.append(
                    ("files", (file_path.name, data, "text/x-python"))
                )
                done += 1
                entry["files_done"] = done
        elif zip_scripts:
            entry["current_file"] = "scripts.zip"
            await asyncio.sleep(0)

            def _zip() -> bytes:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fpath in py_files:
                        zf.write(fpath, arcname=Path(fpath).name)
                return buf.getvalue()

            multipart_files.append(("files", ("scripts.zip", await asyncio.to_thread(_zip), "application/zip")))
            done += 1
            entry["files_done"] = done

        # --- uploading: POST the batch (indeterminate) ---
        entry["phase"] = "uploading"
        entry["status"] = "uploading"
        entry["current_file"] = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=30.0), verify=ssl_verify) as client:
            resp = await client.post(_GALLERY_UPLOAD_URL, data=form_data, files=multipart_files)
            resp.raise_for_status()
        result = resp.json()
        entry["result"] = {
            "upload_id": result.get("upload_id", ""),
            "status_url": result.get("status_url", ""),
            "success": result.get("success", result.get("ok", True)),
            "message": result.get("message", "Submitted successfully"),
        }
        entry["phase"] = "done"
        entry["status"] = "done"
    except httpx.HTTPStatusError as exc:
        entry["status"] = "error"
        entry["phase"] = "error"
        entry["error_code"] = "gallery.dashboard_http_error"
        entry["error_message"] = (
            f"Gallery dashboard returned {exc.response.status_code}: {exc.response.text[:500]}"
        )
    except httpx.RequestError as exc:
        entry["status"] = "error"
        entry["phase"] = "error"
        entry["error_code"] = "gallery.dashboard_unreachable"
        entry["error_message"] = f"Failed to reach gallery dashboard: {exc}"
    except Exception as exc:  # noqa: BLE001 — background task must never leak
        entry["status"] = "error"
        entry["phase"] = "error"
        entry["error_code"] = "gallery.upload_failed"
        entry["error_message"] = f"Upload failed: {exc}"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: DeclarativeContainer) -> APIRouter:
    """Build the gallery submission router.

    Parameters
    ----------
    container
        Application DI container (unused currently but kept for
        interface parity with other route modules).
    """
    router = APIRouter(prefix="/api/gallery", tags=["gallery"])

    # -------------------------------------------------------------------
    # GET /scan
    # -------------------------------------------------------------------

    @router.get("/scan", response_model=GalleryScanResponse)
    async def scan_workdir(
        workdir: str = Query(..., description="Absolute path to model workdir"),
    ) -> GalleryScanResponse:
        """Scan a working directory for gallery-submittable artifacts."""
        work_path = Path(workdir)
        if not work_path.is_dir():
            raise ValidationError(
                "gallery.workdir_not_found",
                f"Directory does not exist: {workdir}",
            )

        files = _collect_files(work_path)
        filenames = [os.path.basename(f.path) for f in files]

        model_name = work_path.name
        qnn_version = _infer_qnn_version(work_path)
        quant_method = _infer_quant_method(filenames)

        # Infer category based on whether DLC files are present
        category = "genai" if any(f.type == "model" for f in files) else "non_genai"

        return GalleryScanResponse(
            model_name=model_name,
            category=category,
            qnn_version=qnn_version,
            quant_method=quant_method,
            files=files,
        )

    # -------------------------------------------------------------------
    # POST /submit
    # -------------------------------------------------------------------

    @router.post("/submit", response_model=GallerySubmitAccepted)
    async def submit_to_gallery(body: GallerySubmitRequest) -> GallerySubmitAccepted:
        """Validate + launch a background packaging/upload to the dashboard.

        Returns immediately with an ``upload_id``; packaging (reading files
        from disk) and the upload run in the background so the frontend can
        poll ``GET /submit-progress/{upload_id}`` for per-file phase progress.
        """
        # Validate that at least one DLC is included.
        dlc_paths = [p for p in body.file_paths if p.lower().endswith(".dlc")]
        if not dlc_paths:
            raise ValidationError(
                "gallery.dlc_required",
                "At least one .dlc file is required for gallery submission.",
            )
        # Validate all paths exist.
        for fpath in body.file_paths:
            if not Path(fpath).is_file():
                raise ValidationError(
                    "gallery.file_not_found",
                    f"File not found: {fpath}",
                )

        # Build form data fields (cheap; no disk reads).
        form_data: dict[str, str] = {
            "submitter": body.submitter,
            "email": body.email,
            "model_name": body.model_name,
            "model_category": body.model_category,
        }
        for field in (
            "model_type",
            "customer",
            "qnn_version",
            "quant_method",
            "scenario",
            "notebook_url",
            "description",
            "performance",
        ):
            value = getattr(body, field, None)
            if value is not None:
                form_data[field] = value

        # Compute the packaging plan (which files get sent individually vs
        # zipped) WITHOUT reading bytes yet — the background task reads them
        # one by one so it can report per-file packaging progress.
        py_files = [p for p in body.file_paths if p.lower().endswith(".py")]
        other_files = [p for p in body.file_paths if not p.lower().endswith(".py")]
        zip_scripts = len(py_files) > 5
        # Ordered list of (label, path-or-None) the packaging phase walks.
        # A ``None`` path with label ``scripts.zip`` means "zip all py_files".
        plan: list[tuple[str, Optional[str]]] = [(Path(p).name, p) for p in other_files]
        if py_files and not zip_scripts:
            plan += [(Path(p).name, p) for p in py_files]
        elif zip_scripts:
            plan.append(("scripts.zip", None))
        files_total = len(plan)

        upload_id = uuid.uuid4().hex
        _UPLOADS[upload_id] = {
            "status": "packaging",
            "phase": "packaging",
            "current_file": plan[0][0] if plan else None,
            "files_done": 0,
            "files_total": files_total,
            "result": None,
            "error_code": None,
            "error_message": None,
        }

        ssl_verify = bool(getattr(getattr(container, "settings", None), "ssl_verify", True))
        task = asyncio.create_task(
            _do_background_upload(
                upload_id=upload_id,
                form_data=form_data,
                other_files=other_files,
                py_files=py_files,
                zip_scripts=zip_scripts,
                ssl_verify=ssl_verify,
            )
        )
        _UPLOAD_TASKS.add(task)
        task.add_done_callback(_UPLOAD_TASKS.discard)

        return GallerySubmitAccepted(upload_id=upload_id, status="packaging")

    # -------------------------------------------------------------------
    # GET /submit-progress/{upload_id}
    # -------------------------------------------------------------------

    @router.get("/submit-progress/{upload_id}", response_model=GallerySubmitProgress)
    async def submit_progress(upload_id: str) -> GallerySubmitProgress:
        """Return the current progress snapshot for a background upload."""
        entry = _UPLOADS.get(upload_id)
        if entry is None:
            raise NotFoundError(
                "gallery.upload_not_found",
                "upload",
                upload_id,
                message=f"Unknown upload_id: {upload_id}",
            )
        return GallerySubmitProgress(**entry)

    # -------------------------------------------------------------------
    # POST /open-file
    # -------------------------------------------------------------------

    @router.post("/open-file")
    async def open_file_in_system(body: _OpenFileRequest):
        """Open a file with the system's default application (Windows only)."""
        file_path = Path(body.path)
        if not file_path.is_file():
            raise NotFoundError(
                "gallery.file_not_found",
                "file",
                body.path,
                message=f"File not found: {body.path}",
            )
        allowed_exts = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".log", ".csv"}
        if file_path.suffix.lower() not in allowed_exts:
            raise ValidationError(
                "gallery.file_type_not_openable",
                f"Cannot open files of type '{file_path.suffix}'",
            )
        os.startfile(str(file_path))  # noqa: S606 — Windows only, intentional
        return {"success": True}

    # -------------------------------------------------------------------
    # POST /delete-file
    # -------------------------------------------------------------------

    @router.post("/delete-file")
    async def delete_file(body: _DeleteFileRequest):
        """Permanently delete a file from the workspace filesystem."""
        file_path = Path(body.path)
        if not file_path.is_file():
            raise NotFoundError(
                "gallery.file_not_found",
                "file",
                body.path,
                message=f"File not found: {body.path}",
            )
        try:
            file_path.unlink()
        except OSError as exc:
            raise ExternalServiceError(
                "gallery.file_delete_failed",
                f"Failed to delete file: {exc}",
                service="filesystem",
                cause=exc,
            ) from exc
        return {"success": True}

    # -------------------------------------------------------------------
    # POST /generate-description
    # -------------------------------------------------------------------

    @router.post("/generate-description", response_model=_GenerateDescriptionResponse)
    async def generate_description(
        body: _GenerateDescriptionRequest,
    ) -> _GenerateDescriptionResponse:
        """Generate a gallery description by calling the user's cloud model.

        Reads the best workspace doc (MODEL_CARD.md → README.md → any .md)
        and asks the resolved cloud model to summarise it. Returns the text
        directly so the UI fills the description field.
        """
        work_path = Path(body.workdir)
        if not work_path.is_dir():
            raise ValidationError(
                "gallery.workdir_not_found",
                f"Directory does not exist: {body.workdir}",
            )
        doc = _find_description_doc(work_path)
        if doc is None:
            raise NotFoundError(
                "gallery.no_doc_found",
                "documentation",
                body.workdir,
                message="No documentation (.md) found in the workspace to summarise.",
            )
        try:
            doc_text = doc.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError as exc:
            raise ExternalServiceError(
                "gallery.doc_read_failed",
                f"Failed to read {doc.name}: {exc}",
                service="filesystem",
                cause=exc,
            ) from exc
        if not doc_text:
            raise ValidationError(
                "gallery.doc_empty",
                f"{doc.name} is empty; nothing to summarise.",
            )
        description = await _generate_description_via_cloud(
            container=container,
            doc_text=doc_text,
            model_id=body.model_id,
        )
        return _GenerateDescriptionResponse(description=description)

    return router


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _guess_mime(ext: str) -> str:
    """Return a reasonable MIME type for gallery file extensions."""
    mime_map = {
        ".dlc": "application/octet-stream",
        ".bin": "application/octet-stream",
        ".md": "text/markdown",
        ".py": "text/x-python",
        ".zip": "application/zip",
    }
    return mime_map.get(ext, "application/octet-stream")
