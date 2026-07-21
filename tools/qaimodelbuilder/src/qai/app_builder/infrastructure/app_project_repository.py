# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed app-project repository (plan §6.4).

Scans ``data/app_builder/*/app.yaml`` and parses each valid app
directory into an :class:`AppProjectDefinition`. This is the App Builder
"standalone fullstack app" registry read path (Phase 2).

Layering (import-linter ``layered-app_builder``): this module lives in
the infrastructure layer and may import domain freely. It deliberately
does NOT import the ``AppProjectRepositoryPort`` protocol (added to
``application/ports.py`` by the DI-wiring step) — the port is a
structural :class:`typing.Protocol`, so :class:`FileSystemAppProjectRepository`
satisfies it by shape (matching method names / signatures) without an
import, keeping infrastructure free of an application-layer dependency
for a pure duck-typed contract. The DI container passes an instance of
this class wherever the port is expected.

Structural contract satisfied (``AppProjectRepositoryPort``)::

    async def list_projects(self) -> list[AppProjectDefinition]
    async def get_project(self, app_id: str) -> AppProjectDefinition

Robustness (plan §1.4 / §5.8, State-Truth-First):

* A malformed / missing / mismatched ``app.yaml`` never raises from
  :meth:`list_projects` — it is logged and skipped so a single bad
  directory can't 500 the whole listing.
* :meth:`get_project` validates the id and resolves the directory under
  ``apps_root`` (path-traversal defence) before reading, translating
  invalid ids / escaping paths / missing dirs into
  :class:`AppProjectNotFoundError` and malformed YAML into
  :class:`AppProjectInvalidError`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

import yaml

from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectDeleteFailedError,
    AppProjectId,
    AppProjectInvalidError,
    AppProjectModelRef,
    AppProjectNotFoundError,
)
from qai.platform.logging import get_logger

logger = get_logger(__name__)

_APP_YAML_NAME = "app.yaml"

# Robust-removal tuning. Kept small + bounded so callers (incl. async ones
# running this in an executor) never block for long.
_RMTREE_MAX_ATTEMPTS = 5
_RMTREE_BASE_DELAY_S = 0.1
_RMTREE_MAX_DELAY_S = 1.6


def _on_rmtree_error(
    func: Any, path: str, _exc_info: Any
) -> None:
    """``shutil.rmtree`` onerror handler: clear readonly bit and retry once.

    Copied model / weight files are frequently marked read-only, which makes
    the OS refuse ``os.unlink`` / ``os.rmdir`` (``PermissionError``). Clearing
    the write-protect bit (``stat.S_IWRITE``) and re-invoking the failed op
    handles that class of failure in-place. Any op that still fails after the
    chmod re-raises so the outer retry loop / final error path can observe it.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        # If we can't even chmod, let the original op re-raise below so the
        # retry loop / final AppProjectDeleteFailedError sees a real OSError.
        pass
    # Retry the failed operation (unlink / rmdir / scandir) exactly once.
    func(path)


def _robust_rmtree(path: Path) -> None:
    """Recursively remove ``path``, surviving transient Windows file locks.

    On Windows, even after a child process tree has been terminated + waited,
    the OS can briefly keep handles open on files it was using (``.py`` /
    ``.pyd`` / ``.dll`` under ``__pycache__``, log files, the listening
    socket). A plain :func:`shutil.rmtree` issued immediately then fails with
    ``PermissionError: [WinError 32] The process cannot access the file
    because it is being used by another process``. Two robustness layers cope
    with that:

    * ``onerror=_on_rmtree_error`` clears the read-only bit on files that
      refuse deletion (common for copied model / weight files) and retries the
      individual failed op once.
    * a short, bounded exponential backoff retries the whole ``rmtree`` up to
      :data:`_RMTREE_MAX_ATTEMPTS` times when the OS is still releasing a
      handle, sleeping ``0.1, 0.2, 0.4, ...`` seconds (capped) between tries.

    Either the tree is fully removed or the final :class:`OSError` propagates
    (never ``ignore_errors=True`` — that would leave a half-deleted dir). This
    is a blocking, synchronous function; ``async`` callers run it in an
    executor so the bounded retries don't stall the event loop.
    """
    delay = _RMTREE_BASE_DELAY_S
    last_exc: OSError | None = None
    for attempt in range(1, _RMTREE_MAX_ATTEMPTS + 1):
        try:
            shutil.rmtree(path, onerror=_on_rmtree_error)
            return
        except OSError as exc:  # incl. PermissionError (WinError 32)
            last_exc = exc
            if attempt >= _RMTREE_MAX_ATTEMPTS:
                break
            logger.info(
                "app_project.rmtree_retry",
                path=str(path),
                attempt=attempt,
                error=str(exc),
            )
            time.sleep(delay)
            delay = min(delay * 2, _RMTREE_MAX_DELAY_S)
    # Exhausted retries — re-raise the final failure for the caller to map.
    assert last_exc is not None  # loop body only exits here after an OSError
    raise last_exc


class FileSystemAppProjectRepository:
    """Read app projects from ``apps_root/<app_id>/app.yaml`` on disk.

    ``apps_root`` is the ``data/app_builder`` directory; the DI container
    resolves it (``data_paths.root / "app_builder"``) and passes it in.
    The repository does not resolve paths from :class:`DataPaths` itself
    to keep it a pure, easily-testable filesystem adapter.
    """

    def __init__(self, *, apps_root: Path) -> None:
        if not isinstance(apps_root, Path):
            raise TypeError("apps_root must be a Path")
        self._apps_root = apps_root

    # ------------------------------------------------------------------
    # Port methods
    # ------------------------------------------------------------------
    async def list_projects(self) -> list[AppProjectDefinition]:
        """Return all valid app projects, newest-first by dir mtime.

        Malformed / invalid app directories are logged and skipped (never
        raised) so the listing is robust on a fresh / partially-written
        data dir.
        """
        root = self._apps_root
        if not root.is_dir():
            return []

        projects: list[AppProjectDefinition] = []
        try:
            entries = list(root.iterdir())
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                "app_project.list_scan_failed",
                apps_root=str(root),
                error=str(exc),
            )
            return []

        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:  # pragma: no cover - defensive
                continue
            definition = self._parse_app_yaml(entry)
            if definition is not None:
                projects.append(definition)

        # Newest-first by directory mtime.
        projects.sort(key=lambda d: d.modified_at, reverse=True)
        return projects

    async def get_project(self, app_id: str) -> AppProjectDefinition:
        """Return one app project by id.

        Raises :class:`AppProjectNotFoundError` when the id is invalid,
        escapes ``apps_root``, or the directory does not exist; raises
        :class:`AppProjectInvalidError` when the directory exists but its
        ``app.yaml`` is missing / malformed / id-mismatched.
        """
        # 1) Validate id shape. An invalid id is treated as "not found"
        #    (path-traversal defence: only well-formed ids can name a dir).
        try:
            validated = AppProjectId(value=app_id)
        except (ValueError, TypeError) as exc:
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            ) from exc

        # 2) Resolve + assert containment under apps_root.
        app_dir = self._apps_root / validated.value
        try:
            resolved = app_dir.resolve()
            root_resolved = self._apps_root.resolve()
        except OSError as exc:  # pragma: no cover - defensive
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            ) from exc
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            )

        # 3) Directory must exist.
        if not app_dir.is_dir():
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            )

        # 4) app.yaml must exist + parse + validate. get() raises (unlike
        #    list which skips) so the route can surface a clean 400.
        definition = self._parse_app_yaml(app_dir)
        if definition is None:
            raise AppProjectInvalidError(
                message=(
                    f"app project {app_id!r} has a missing or invalid "
                    f"{_APP_YAML_NAME}"
                ),
                details={"app_id": app_id},
            )
        return definition

    async def delete_project(self, app_id: str) -> None:
        """Delete the app project directory ``apps_root/<app_id>/`` (recursive).

        Path-traversal safe: the id must be well-formed AND the resolved dir
        must be contained STRICTLY under ``apps_root`` (same guard as
        :meth:`get_project`, but the root itself is refused) before anything
        is removed — a malformed id or a dir that escapes the root raises
        :class:`AppProjectNotFoundError` and deletes NOTHING. Only the on-disk
        *development* project under ``data/app_builder/`` is removed; packaged
        zips in the workspace are never touched. A missing dir raises NotFound
        (the route maps it to 404) rather than silently succeeding.
        """
        # 1) Validate id shape (path-traversal defence).
        try:
            validated = AppProjectId(value=app_id)
        except (ValueError, TypeError) as exc:
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            ) from exc

        # 2) Resolve + assert containment STRICTLY under apps_root. The
        #    resolved dir must be a real child of the root (not the root
        #    itself) — we never rmtree the apps root.
        app_dir = self._apps_root / validated.value
        try:
            resolved = app_dir.resolve()
            root_resolved = self._apps_root.resolve()
        except OSError as exc:  # pragma: no cover - defensive
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            ) from exc
        if root_resolved not in resolved.parents:
            # Escapes the root, or IS the root — refuse.
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            )

        # 3) Directory must exist.
        if not app_dir.is_dir():
            raise AppProjectNotFoundError(
                message=f"app project {app_id!r} not found",
                details={"app_id": app_id},
            )

        # 4) Remove the tree. Translate IO failures to a domain error so the
        #    route can surface a clean 5xx instead of leaking OSError. The
        #    removal is robust against transient Windows file locks (WinError
        #    32 — the just-stopped uvicorn process tree may briefly still hold
        #    handles on __pycache__ .pyd/.dll, log files, or the socket) and
        #    read-only files; see :func:`_robust_rmtree`. It's a blocking,
        #    bounded-retry call, so run it in the default executor to avoid
        #    stalling the event loop across the (short) backoffs.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _robust_rmtree, app_dir)
        except OSError as exc:
            raise AppProjectDeleteFailedError(
                message=(
                    f"failed to delete app project {app_id!r} "
                    f"at {app_dir}: {exc}"
                ),
                details={"app_id": app_id, "path": str(app_dir)},
            ) from exc

    # ------------------------------------------------------------------
    # Parsing (shared by list + get; returns None on any failure)
    # ------------------------------------------------------------------
    def _parse_app_yaml(self, app_dir: Path) -> AppProjectDefinition | None:  # noqa: PLR0911 - lenient parse gate; each early return maps a distinct YAML failure to None
        """Parse ``app_dir/app.yaml`` → :class:`AppProjectDefinition`.

        Returns ``None`` on ANY failure (missing file, unreadable, bad
        YAML, missing/mismatched id, id fails regex, structurally
        invalid). Callers that must raise (``get_project``) translate the
        ``None`` into the appropriate domain error.
        """
        yaml_path = app_dir / _APP_YAML_NAME
        try:
            if not yaml_path.is_file():
                return None
            raw_text = yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.info(
                "app_project.yaml_unreadable",
                app_dir=str(app_dir),
                error=str(exc),
            )
            return None

        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            logger.info(
                "app_project.yaml_malformed",
                app_dir=str(app_dir),
                error=str(exc),
            )
            return None

        if not isinstance(data, dict):
            logger.info(
                "app_project.yaml_not_mapping",
                app_dir=str(app_dir),
            )
            return None

        # id must be present, match the regex, AND equal the dir name.
        raw_id = data.get("id")
        if not isinstance(raw_id, str):
            logger.info("app_project.yaml_missing_id", app_dir=str(app_dir))
            return None
        try:
            app_id = AppProjectId(value=raw_id)
        except ValueError as exc:
            logger.info(
                "app_project.yaml_invalid_id",
                app_dir=str(app_dir),
                raw_id=raw_id,
                error=str(exc),
            )
            return None
        if app_id.value != app_dir.name:
            logger.info(
                "app_project.yaml_id_dir_mismatch",
                app_dir=str(app_dir),
                yaml_id=app_id.value,
                dir_name=app_dir.name,
            )
            return None

        try:
            mtime = app_dir.stat().st_mtime
        except OSError:  # pragma: no cover - defensive
            mtime = 0.0

        try:
            models = self._parse_models(data.get("models"))
            entry = data.get("entry") if isinstance(data.get("entry"), dict) else {}
            runtime = (
                data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
            )
            package = (
                data.get("package") if isinstance(data.get("package"), dict) else {}
            )

            definition = AppProjectDefinition(
                id=app_id,
                name=self._as_str(data.get("name"), default=app_id.value),
                description=self._as_str(data.get("description"), default=""),
                models=models,
                app_module=self._as_str(
                    entry.get("app_module"), default="backend.main:app"
                ),
                health_path=self._as_str(
                    entry.get("health_path"), default="/health"
                ),
                frontend_path=self._as_str(
                    entry.get("frontend_path"), default="/"
                ),
                host=self._as_str(runtime.get("host"), default="127.0.0.1"),
                preferred_port=self._as_optional_int(runtime.get("preferred_port")),
                created_at=self._as_optional_str(data.get("created_at")),
                updated_at=self._as_optional_str(data.get("updated_at")),
                package_include_models=self._as_bool(
                    package.get("include_models"), default=True
                ),
                package_include_outputs=self._as_bool(
                    package.get("include_outputs"), default=False
                ),
                path=str(app_dir.resolve()),
                modified_at=float(mtime),
            )
        except (ValueError, TypeError) as exc:
            logger.info(
                "app_project.definition_invalid",
                app_dir=str(app_dir),
                error=str(exc),
            )
            return None

        return definition

    # ------------------------------------------------------------------
    # Coercion helpers (lenient — the schema is user/LLM-authored YAML)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_models(raw: Any) -> tuple[AppProjectModelRef, ...]:
        if not isinstance(raw, list):
            return ()
        out: list[AppProjectModelRef] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, str) or not mid.strip():
                continue
            title = item.get("title")
            pack_dir = item.get("pack_dir")
            model_dir = item.get("model_dir")
            out.append(
                AppProjectModelRef(
                    id=mid,
                    title=title if isinstance(title, str) else mid,
                    builtin=bool(item.get("builtin", False)),
                    pack_dir=pack_dir if isinstance(pack_dir, str) else None,
                    model_dir=model_dir if isinstance(model_dir, str) else None,
                )
            )
        return tuple(out)

    @staticmethod
    def _as_str(value: Any, *, default: str) -> str:
        return value if isinstance(value, str) and value else default

    @staticmethod
    def _as_optional_str(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _as_bool(value: Any, *, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    @staticmethod
    def _as_optional_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None


__all__ = ["FileSystemAppProjectRepository"]
