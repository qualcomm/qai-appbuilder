# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed app-project packager (plan §2.4 / §5.6 / §10.4).

Builds a distributable ``.zip`` of a generated standalone app project —
the app's own code (backend / frontend / launch scripts / ``app.yaml``)
plus, optionally, the minimal model + weight set each bundled model needs
to run on a target machine — and drops it under the user workspace at
``<workspace>/app_builder_packages/<app_id>-<YYYYMMDD-HHMMSS>.zip``.

Layering (import-linter ``layered-app_builder``): this module lives in
the infrastructure layer and imports domain freely. It deliberately does
NOT import the ``AppProjectPackagerPort`` protocol (added to
``application/ports.py`` by the DI-wiring step) — the port is a
structural :class:`typing.Protocol`, so :class:`FileSystemAppProjectPackager`
satisfies it by shape (matching method name / signature) without an
import, keeping infrastructure free of an application-layer dependency
for a pure duck-typed contract. The DI container passes an instance of
this class wherever the port is expected.

Structural contract satisfied (``AppProjectPackagerPort``)::

    def package(
        self, definition: AppProjectDefinition
    ) -> AsyncIterator[PackageProgress]

Design (progressive, cancel-safe, path-safe)
--------------------------------------------
* :meth:`package` is an **async generator** yielding :class:`PackageProgress`
  snapshots so the SSE route can stream live phase / percent updates while
  a large weight copy runs. The final snapshot has ``is_complete=True``
  carrying ``zip_path`` + ``size_bytes``.
* The zip is built into a temp file first, then atomically moved into the
  ``app_builder_packages`` dir — a crash / cancel mid-build never leaves a
  half-written ``<app_id>-<ts>.zip`` for the UI to offer as "done".
* Path safety (plan §5.8): the app dir MUST resolve under ``apps_root``
  and every member added to the zip MUST resolve under the app dir (no
  symlink escape). Model weight copy paths MUST resolve under
  ``repo_root/models`` or the pack root.
* ``${APP_ROOT}`` placeholders in a model ref's ``model_dir`` / ``pack_dir``
  are expanded against the injected ``repo_root`` (see :meth:`_expand`).
"""

from __future__ import annotations

import asyncio
import json
import os
import zipfile
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from qai.app_builder.domain.app_project import (
    AppProjectDefinition,
    AppProjectModelRef,
    AppProjectPackageFailedError,
)
from qai.platform.logging import get_logger
from qai.platform.process.arch import current_arch

logger = get_logger(__name__)

__all__ = [
    "FileSystemAppProjectPackager",
    "PackageProgress",
]

# Placeholder token the ``app.yaml`` model refs use for the repo root.
_APP_ROOT_TOKEN = "${APP_ROOT}"  # noqa: S105 - path placeholder token, not a secret

# Top-level app entries always packaged when present (files + dirs). The
# member paths in the zip are rooted at the app id's parent (i.e. the zip
# stores ``backend/main.py`` etc., not ``<app_id>/backend/main.py``).
_APP_INCLUDE_FILES = (
    "README.md",
    "run.bat",
    "run.ps1",
    "run.sh",
    "requirements.txt",
    "app.yaml",
)
# ``vendor`` (when present) carries offline / ARM64 wheels + runtime data the
# startup dependency check (``backend/ensure_deps.py``) installs from — kept
# beside the app so a bare target machine can self-heal without network access.
_APP_INCLUDE_DIRS = ("backend", "frontend", "vendor")

# Directory names excluded ANYWHERE in the app tree (venvs / caches / the
# package output dir itself / user uploads / logs). Matched case-insensitively
# on the directory *name* so ``.venv`` deep in ``backend/`` is skipped too.
_EXCLUDE_DIR_NAMES = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        "package",  # the app's own package output dir
        "uploads",  # user uploads — never redistribute
        "node_modules",
    }
)

# File suffixes excluded anywhere.
_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})

# ``logs/`` is excluded only for its large ``*.log`` bodies; the dir itself
# and small non-log files may pass, but in practice we skip the whole dir.
_EXCLUDE_LOG_DIR = "logs"

# Project-level shared-helper dir name. It sits beside ``pack_root`` at
# ``<repo>/factory/chat_features/app-builder/shared`` and holds the framework modules the
# generated app's in-process runner import needs (``runner_protocol``,
# ``telemetry``, ``audio_io``, ``weight_downloader`` …). See
# :meth:`_collect_shared_members`.
_SHARED_DIRNAME = "shared"


@dataclass(frozen=True, slots=True, kw_only=True)
class PackageProgress:
    """One progress snapshot yielded while packaging an app project.

    ``phase`` is a coarse machine label (``"collecting"`` / ``"copying_app"``
    / ``"copying_models"`` / ``"writing_zip"`` / ``"done"``); ``percent`` is a
    ``0..100`` float estimate; ``message`` is a short human line. The terminal
    snapshot sets ``is_complete=True`` and carries the final ``zip_path`` +
    ``size_bytes`` (both ``None`` on every non-terminal snapshot).
    """

    phase: str
    percent: float
    message: str
    zip_path: str | None = None
    size_bytes: int | None = None
    is_complete: bool = False


class FileSystemAppProjectPackager:
    """Package a standalone app project into a workspace-rooted ``.zip``.

    ``repo_root`` anchors ``${APP_ROOT}`` expansion + the built-in model /
    pack roots (``repo_root/models``, ``repo_root/factory/chat_features/app-builder/models``).
    ``workspace_root`` is the user workspace the final zip lands under
    (``<workspace>/app_builder_packages/``). ``apps_root`` is one path OR
    an iterable of paths every app must resolve under (path-safety
    containment); pass every root the repository scans (typically
    ``data/app_builder`` for user-generated apps and
    ``samples/app_builder`` for built-in demos) — otherwise sample apps
    fail containment before the first progress yield. ``clock`` returns
    the timestamp used in the zip file name (injectable for
    deterministic tests; defaults to ``datetime.now``).

    Dual-anchor support (built-in + user Pack roots) — P4 分层
    ---------------------------------------------------------
    Since P4 the runtime tracks two sets of Pack anchors (与 packager 外的
    ``FileSystemWeightsPresence`` / ``FilesystemSkillPathLocator`` 双 anchor
    语义一致，State-Truth-First §5 铁律 1):

    * **built-in** — ``model_root`` (``<repo>/models``) + ``pack_root``
      (``<repo>/factory/chat_features/app-builder/models``);
    * **user-imported** — ``user_weights_root``
      (``<data>/app_builder/user_model_weights``) + ``user_pack_root``
      (``<data>/app_builder/user_models``).

    Weight / pack directories may resolve under **either** anchor pair; the
    containment check accepts a path that lies under any of the four roots.
    Missing user anchors (``None``) degrade to built-in-only behaviour so
    lean test containers keep working.

    **Arcnames stay unchanged** (``models/<id>/…`` and ``pack/<id>/…``) so
    the on-disk layout inside the packaged zip is identical for built-in
    and user packs — the generated ``_resolve_dir`` in the app's
    ``inference.py`` finds either via the same ancestor-walk fallback.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        workspace_root: Path,
        apps_root: Path | Iterable[Path],
        user_pack_root: Path | None = None,
        user_weights_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._workspace_root = Path(workspace_root)
        # Accept a single Path (legacy) or an iterable of roots. The
        # repository already scans multiple roots (data/app_builder for
        # user-generated apps + samples/app_builder for built-in demos);
        # the packager MUST accept an app dir resolving under ANY of them
        # or built-in demo apps get rejected with "escapes apps root"
        # before the first progress yield (silent-close bug in the WS
        # handler manifested as "connection open → connection closed").
        if isinstance(apps_root, Path):
            self._apps_roots: tuple[Path, ...] = (Path(apps_root),)
        else:
            roots = tuple(Path(r) for r in apps_root)
            if not roots:
                raise ValueError("apps_root must contain at least one path")
            self._apps_roots = roots
        self._user_pack_root = (
            Path(user_pack_root) if user_pack_root is not None else None
        )
        self._user_weights_root = (
            Path(user_weights_root) if user_weights_root is not None else None
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def model_root(self) -> Path:
        """``APP_BUILDER_MODEL_ROOT`` — built-in weight tree (``repo_root/models``)."""
        return self._repo_root / "models"

    @property
    def pack_root(self) -> Path:
        """``APP_BUILDER_PACK_ROOT`` — built-in pack manifests / assets tree."""
        return self._repo_root / "factory" / "chat_features" / "app-builder" / "models"

    @property
    def user_weights_root(self) -> Path | None:
        """``APP_BUILDER_USER_MODEL_ROOT`` — user-imported weight tree.

        User weight files live at ``user_weights_root/models/<id>/<bin>``
        (note the extra ``models/`` layer, matching the manifest
        ``installPath = "models/<id>/<bin>"`` convention).
        """
        return self._user_weights_root

    @property
    def user_pack_root(self) -> Path | None:
        """``APP_BUILDER_USER_PACK_ROOT`` — user-imported pack tree.

        User pack directories live at ``user_pack_root/<id>/…`` (no extra
        ``models/`` layer — the pack IS the ``<id>`` dir).
        """
        return self._user_pack_root

    # ── public port method ───────────────────────────────────────────────

    async def package(  # noqa: PLR0915 - cohesive progressive generator: sequential build phases with interleaved progress yields
        self, definition: AppProjectDefinition
    ) -> AsyncIterator[PackageProgress]:
        """Build the app package, yielding progress until ``is_complete``.

        Raises :class:`AppProjectPackageFailedError` (``app_builder.package_failed``)
        on a path-escape / IO failure BEFORE the first yield; a missing model
        / pack path mid-build is recorded as a warning in the manifest and
        skipped (never crashes the whole package).
        """
        app_id = definition.id.value
        app_dir = self._resolve_app_dir(definition)

        # ── collecting: plan the member set + estimate size ───────────────
        yield PackageProgress(
            phase="collecting",
            percent=0.0,
            message=f"collecting files for {app_id!r}",
        )
        app_members = self._collect_app_members(app_dir)
        warnings: list[str] = []
        model_plan: list[tuple[AppProjectModelRef, list[tuple[Path, str]]]] = []
        shared_members: list[tuple[Path, str]] = []
        if definition.package_include_models:
            for ref in definition.models:
                members = self._collect_model_members(ref, warnings)
                model_plan.append((ref, members))
            # Framework helpers the in-process pack runner imports by bare name
            # (runner_protocol / telemetry / …). Bundled once regardless of the
            # model count. See :meth:`_collect_shared_members`.
            shared_members = self._collect_shared_members(warnings)

        app_bytes = _sum_size(m[0] for m in app_members)
        model_bytes = _sum_size(
            src for _, members in model_plan for src, _ in members
        ) + _sum_size(src for src, _ in shared_members)
        total_bytes = max(app_bytes + model_bytes, 1)
        copied = 0

        # ── build the zip into a temp file ────────────────────────────────
        out_dir = self._workspace_root / "app_builder_packages"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppProjectPackageFailedError(
                message=f"cannot create package output dir {out_dir}: {exc}",
            ) from exc

        stamp = self._clock().strftime("%Y%m%d-%H%M%S")
        final_path = out_dir / f"{app_id}-{stamp}.zip"
        tmp_path = out_dir / f".{app_id}-{stamp}.zip.part"

        model_ids = [ref.id for ref, _ in model_plan]

        try:
            with zipfile.ZipFile(
                tmp_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                # App files. Each ``zf.write`` is BLOCKING disk+deflate IO; we
                # run it via ``asyncio.to_thread`` and ``await`` so the event
                # loop stays free and the StreamingResponse can actually FLUSH
                # each progress frame to the browser as we go (otherwise the
                # whole multi-hundred-MB copy blocks the loop and every frame
                # arrives at once at the very end → the bar sat at 0%). We also
                # yield progress periodically DURING the copy (per file for big
                # model weights, throttled for many small app files) so the
                # percent advances smoothly instead of jumping 0 → 92.
                yield PackageProgress(
                    phase="copying_app",
                    percent=_pct(copied, total_bytes) * 0.9,
                    message="packaging app files",
                )
                _since_yield = 0
                for src, arcname in app_members:
                    await asyncio.to_thread(zf.write, src, arcname)
                    copied += _safe_size(src)
                    _since_yield += 1
                    # App files are usually small + numerous; yield every few
                    # so the bar moves without flooding the SSE stream.
                    if _since_yield >= 8:
                        _since_yield = 0
                        yield PackageProgress(
                            phase="copying_app",
                            percent=_pct(copied, total_bytes) * 0.9,
                            message="packaging app files",
                        )
                        await asyncio.sleep(0)  # let the response flush

                # Model weights + pack manifests. Weight files are large, so
                # yield after EACH file for a responsive bar.
                if model_plan:
                    yield PackageProgress(
                        phase="copying_models",
                        percent=_pct(copied, total_bytes) * 0.9,
                        message=f"packaging {len(model_plan)} model(s)",
                    )
                    await asyncio.sleep(0)
                    for ref, members in model_plan:
                        for src, arcname in members:
                            await asyncio.to_thread(zf.write, src, arcname)
                            copied += _safe_size(src)
                            yield PackageProgress(
                                phase="copying_models",
                                percent=_pct(copied, total_bytes) * 0.9,
                                message=f"packaging model {ref.id!r}",
                            )
                            await asyncio.sleep(0)  # let the response flush
                        yield PackageProgress(
                            phase="copying_models",
                            percent=_pct(copied, total_bytes) * 0.9,
                            message=f"packaged model {ref.id!r}",
                        )

                # Framework shared helpers (runner_protocol / telemetry / …)
                # the in-process pack runner imports. Small + numerous; yield
                # periodically like the app files.
                if shared_members:
                    yield PackageProgress(
                        phase="copying_models",
                        percent=_pct(copied, total_bytes) * 0.9,
                        message="packaging shared helpers",
                    )
                    _since_yield = 0
                    for src, arcname in shared_members:
                        await asyncio.to_thread(zf.write, src, arcname)
                        copied += _safe_size(src)
                        _since_yield += 1
                        if _since_yield >= 8:
                            _since_yield = 0
                            yield PackageProgress(
                                phase="copying_models",
                                percent=_pct(copied, total_bytes) * 0.9,
                                message="packaging shared helpers",
                            )
                            await asyncio.sleep(0)

                # Manifest + running doc (small, computed last).
                yield PackageProgress(
                    phase="writing_zip",
                    percent=92.0,
                    message="writing package manifest",
                )
                manifest = _build_manifest(
                    app_id=app_id,
                    definition=definition,
                    model_ids=model_ids,
                    total_size_bytes=app_bytes + model_bytes,
                    packaged_at=self._clock(),
                    warnings=warnings,
                )
                zf.writestr(
                    "package_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                zf.writestr("RUNNING.md", _running_md())
                # Generated run.bat with port-fallback (preferred port
                # tried first; automatic fallback if occupied).
                zf.writestr(
                    "run.bat",
                    _run_bat_content(
                        host=definition.host,
                        preferred_port=definition.preferred_port or 8000,
                    ),
                )
                # Enhanced requirements.txt: original app requirements + pinned
                # qai-appbuilder so ensure_deps.py upgrades a stale SDK on the
                # target machine (prevents "newer binary on old SDK" crash).
                zf.writestr(
                    "requirements.txt",
                    _enhanced_requirements(app_dir),
                )
        except AppProjectPackageFailedError:
            _unlink_quiet(tmp_path)
            raise
        except OSError as exc:
            _unlink_quiet(tmp_path)
            raise AppProjectPackageFailedError(
                message=f"failed to write package for {app_id!r}: {exc}",
            ) from exc

        # ── atomic move into place ─────────────────────────────────────────
        try:
            if final_path.exists():
                final_path.unlink()
            os.replace(tmp_path, final_path)
        except OSError as exc:
            _unlink_quiet(tmp_path)
            raise AppProjectPackageFailedError(
                message=f"failed to finalize package for {app_id!r}: {exc}",
            ) from exc

        size_bytes = _safe_size(final_path)
        yield PackageProgress(
            phase="done",
            percent=100.0,
            message=f"packaged {app_id!r} ({size_bytes} bytes)",
            zip_path=str(final_path),
            size_bytes=size_bytes,
            is_complete=True,
        )

    # ── path safety ────────────────────────────────────────────────────

    def _resolve_app_dir(self, definition: AppProjectDefinition) -> Path:
        """Resolve + containment-check the app dir under ``apps_roots``.

        Uses ``definition.path`` (the repository-filled absolute app dir).
        Raises :class:`AppProjectPackageFailedError` when the resolved dir
        escapes every configured root or does not exist.
        """
        try:
            app_dir = Path(definition.path).resolve()
            resolved_roots = tuple(r.resolve() for r in self._apps_roots)
        except OSError as exc:
            raise AppProjectPackageFailedError(
                message=f"cannot resolve app dir for {definition.id.value!r}: {exc}",
            ) from exc
        if not any(
            app_dir == root or root in app_dir.parents
            for root in resolved_roots
        ):
            roots_display = ", ".join(str(r) for r in resolved_roots)
            raise AppProjectPackageFailedError(
                message=f"app dir {app_dir} escapes apps roots [{roots_display}]",
            )
        if not app_dir.is_dir():
            raise AppProjectPackageFailedError(
                message=f"app dir {app_dir} does not exist",
            )
        return app_dir

    def _expand(self, raw: str) -> Path:
        """Expand ``${APP_ROOT}`` in ``raw`` against ``repo_root`` + resolve."""
        replaced = raw.replace(_APP_ROOT_TOKEN, str(self._repo_root))
        return Path(replaced).resolve()

    @staticmethod
    def _is_under(child: Path, parent: Path) -> bool:
        """Whether ``child`` resolves at / under ``parent`` (both resolved)."""
        try:
            child_r = child.resolve()
            parent_r = parent.resolve()
        except OSError:
            return False
        return child_r == parent_r or parent_r in child_r.parents

    # ── member collection ────────────────────────────────────────────────

    def _collect_app_members(self, app_dir: Path) -> list[tuple[Path, str]]:
        """Return ``(src_path, arcname)`` pairs for the app's own files.

        Applies the exclude whitelist (venvs / caches / package / uploads /
        logs) and refuses to follow a symlink that escapes ``app_dir`` (plan
        §5.8). Arcnames are POSIX-relative to ``app_dir``.
        """
        members: list[tuple[Path, str]] = []

        for name in _APP_INCLUDE_FILES:
            # Skip run.bat — the packager generates a port-fallback version
            # via _run_bat_content() written directly into the zip.
            # Skip requirements.txt — the packager generates an enhanced version
            # that pins qai-appbuilder to ensure SDK/model version compatibility.
            if name in ("run.bat", "requirements.txt"):
                continue
            src = app_dir / name
            if src.is_file() and self._is_under(src, app_dir):
                members.append((src, name))

        for dirname in _APP_INCLUDE_DIRS:
            base = app_dir / dirname
            if not base.is_dir():
                continue
            members.extend(self._walk_dir(base, app_dir))

        return members

    def _walk_dir(
        self, base: Path, app_dir: Path
    ) -> list[tuple[Path, str]]:
        """Walk ``base`` (a dir under ``app_dir``) applying the exclude rules."""
        out: list[tuple[Path, str]] = []
        for root, dirs, files in os.walk(base):
            root_path = Path(root)
            # Prune excluded directories in-place so os.walk never descends.
            dirs[:] = [
                d for d in dirs if d.casefold() not in _EXCLUDE_DIR_NAMES
            ]
            # Skip anything under a logs/ dir (large rotating logs).
            rel_parts = {p.casefold() for p in root_path.relative_to(app_dir).parts}
            if _EXCLUDE_LOG_DIR in rel_parts:
                continue
            for fname in files:
                if Path(fname).suffix.casefold() in _EXCLUDE_SUFFIXES:
                    continue
                src = root_path / fname
                # Symlink-escape guard: a member must resolve under app_dir.
                if src.is_symlink() and not self._is_under(src, app_dir):
                    logger.warning(
                        "app_project_packager: skip escaping symlink %s", src
                    )
                    continue
                if not src.is_file():
                    continue
                arcname = src.relative_to(app_dir).as_posix()
                out.append((src, arcname))
        return out

    def _collect_model_members(
        self, ref: AppProjectModelRef, warnings: list[str]
    ) -> list[tuple[Path, str]]:
        """Return ``(src, arcname)`` pairs for one model ref's weights + pack.

        Weights land under ``models/<id>/`` in the zip; pack manifests /
        assets under ``pack/<id>/``. Missing paths are recorded in
        ``warnings`` and skipped (never raised) so one absent model cannot
        abort the whole package (plan §10.4).
        """
        out: list[tuple[Path, str]] = []
        out.extend(self._collect_model_weights(ref, warnings))
        out.extend(self._collect_model_pack(ref, warnings))
        return out

    def _collect_model_weights(
        self, ref: AppProjectModelRef, warnings: list[str]
    ) -> list[tuple[Path, str]]:
        """Weight files for ``ref`` under ``models/<id>/`` (plan §10.4).

        P4 双根：weights 可能落在 built-in ``model_root``（``<repo>/models/<id>/``）
        或 user ``user_weights_root``（``<data>/app_builder/user_model_weights/
        models/<id>/``）。containment check 接受任一根，fallback 探测顺序：
        built-in 先查 → miss 再查 user（与运行时探测语义一致）。
        Arcname 保持 ``models/<id>/…``——zip 内部结构对两种来源统一，
        目标机上生成的 ``_resolve_dir`` 无需区分来源。

        Expand-miss fallback（防御 LLM 生成的 app.yaml 硬编码 built-in 路径）：
        当 ``ref.model_dir`` 展开后指向 built-in 层但磁盘上不存在（典型
        错误：``${APP_ROOT}/models/inception-v3`` for a user pack），
        packager **不直接放弃**——先按 ``ref.id`` 走 ``_fallback_weights_dir``
        再探测 user root。这样即使 LLM 按 SKILL 的老 yaml 模板生成了
        单根路径，只要 pack 实际存在于 user root，打包仍能成功。
        """
        out: list[tuple[Path, str]] = []
        # Weights: expand model_dir (or fall back through the four roots).
        if ref.model_dir:
            model_dir = self._expand(ref.model_dir)
            # Expand-miss fallback: 展开后不存在 → 再走双根 fallback
            # 探测（既保留 explicit model_dir 的优先级，又救 LLM 硬编码
            # 单根的常见错误，见 docstring）。
            if not model_dir.is_dir():
                fallback = self._fallback_weights_dir(ref.id)
                if fallback.is_dir():
                    model_dir = fallback
        else:
            model_dir = self._fallback_weights_dir(ref.id)
        if not self._is_under_any_weights_root(model_dir):
            warnings.append(
                f"model {ref.id!r}: model_dir {model_dir} outside built-in "
                f"({self.model_root}) and user "
                f"({self._user_weights_root}) weights roots; skipped"
            )
        elif not model_dir.is_dir():
            warnings.append(
                f"model {ref.id!r}: weights dir {model_dir} missing "
                f"(also tried fallback under user root "
                f"{self._user_weights_root}); skipped"
            )
        else:
            for src in _iter_files(model_dir):
                rel = src.relative_to(model_dir).as_posix()
                out.append((src, f"models/{ref.id}/{rel}"))
        return out

    def _fallback_weights_dir(self, model_id: str) -> Path:
        """Locate a ref's weights dir when ``ref.model_dir`` is empty.

        Probes built-in first, then user. Returns the first existing dir; if
        neither exists, returns the built-in candidate so the caller records
        a "missing" warning against the canonical path.
        """
        candidates: list[Path] = [(self.model_root / model_id).resolve()]
        if self._user_weights_root is not None:
            # User weights layout: user_weights_root/models/<id>/
            candidates.append(
                (self._user_weights_root / "models" / model_id).resolve()
            )
        for c in candidates:
            if c.is_dir():
                return c
        return candidates[0]

    def _is_under_any_weights_root(self, path: Path) -> bool:
        """Whether ``path`` lies under built-in ``model_root`` OR
        ``user_weights_root``。missing anchors are ignored (degrade to
        built-in-only)."""
        if self._is_under(path, self.model_root):
            return True
        if self._user_weights_root is not None and self._is_under(
            path, self._user_weights_root
        ):
            return True
        return False

    def _collect_model_pack(
        self, ref: AppProjectModelRef, warnings: list[str]
    ) -> list[tuple[Path, str]]:
        """Pack sources for ``ref`` under ``pack/<id>/`` (plan §10.4).

        P4 双根：pack 目录可能落在 built-in ``pack_root``
        （``<repo>/factory/chat_features/app-builder/models/<id>/``）或 user
        ``user_pack_root``（``<data>/app_builder/user_models/<id>/``）。
        Arcname 保持 ``pack/<id>/…``——zip 内部对两种来源统一。

        Expand-miss fallback: 与 ``_collect_model_weights`` 同理，防御
        LLM 生成 app.yaml 硬编码单根 pack_dir 的常见错误。

        **Bundle the WHOLE pack dir (caches excluded), not a file whitelist.**
        The generated app reuses the pack's ``runner.py`` *in-process*
        (``import runner``), so every pack-local module the runner imports —
        e.g. ``melo_zh_local/``, ``bert_tokenizer_local.py``,
        ``melo_symbol_to_id.json`` — MUST ride along, or the packaged app
        dies at import with ``ModuleNotFoundError``. Heavy weight ``.bin`` /
        ``.onnx`` files are copied separately under ``models/<id>/`` (from
        ``model_dir``), so a full pack walk here does not double-ship weights
        for well-formed packs; the ``__pycache__`` / cache dirs are pruned by
        the shared exclude rules in :func:`_iter_files`.
        """
        out: list[tuple[Path, str]] = []
        # Pack: expand pack_dir (or fall back through the four roots).
        if ref.pack_dir:
            pack_dir = self._expand(ref.pack_dir)
            if not pack_dir.is_dir():
                fallback = self._fallback_pack_dir(ref.id)
                if fallback.is_dir():
                    pack_dir = fallback
        else:
            pack_dir = self._fallback_pack_dir(ref.id)
        if not self._is_under_any_pack_root(pack_dir):
            warnings.append(
                f"model {ref.id!r}: pack_dir {pack_dir} outside built-in "
                f"({self.pack_root}) and user "
                f"({self._user_pack_root}) pack roots; skipped"
            )
        elif not pack_dir.is_dir():
            warnings.append(
                f"model {ref.id!r}: pack dir {pack_dir} missing "
                f"(also tried fallback under user root "
                f"{self._user_pack_root}); skipped"
            )
        else:
            for src in _iter_files(pack_dir):
                rel = src.relative_to(pack_dir).as_posix()
                out.append((src, f"pack/{ref.id}/{rel}"))
        return out

    def _collect_shared_members(
        self, warnings: list[str]
    ) -> list[tuple[Path, str]]:
        """Framework shared-helper modules bundled under ``shared/`` in the zip.

        The generated app imports the pack's ``runner.py`` in-process, and that
        runner imports project-level framework modules by bare name —
        ``runner_protocol``, ``telemetry``, ``audio_io``, ``weight_downloader``,
        ``qnn_helper`` … — which live at ``<repo>/factory/chat_features/app-builder/shared``
        and are normally injected on ``PYTHONPATH`` by the dev host. A packaged
        app on another machine has no such host, so the generated
        ``model_refs.resolve_shared_dir()`` walks up from ``backend/`` looking
        for a bundled ``<pkg>/shared/runner_protocol.py``. This copies that dir
        in so the walk-up succeeds; without it the app dies at startup with
        ``ModuleNotFoundError: No module named 'runner_protocol'``.

        Absent ``shared/`` (lean test repo) is recorded as a warning, not a
        crash — apps that re-implement inference without the pack runner do not
        need it.
        """
        out: list[tuple[Path, str]] = []
        shared_dir = (self.pack_root.parent / _SHARED_DIRNAME).resolve()
        if not shared_dir.is_dir():
            warnings.append(
                f"shared helper dir {shared_dir} not found; packaged app "
                f"may fail to import runner_protocol / telemetry if it reuses "
                f"the pack runner in-process"
            )
            return out
        for src in _iter_files(shared_dir):
            rel = src.relative_to(shared_dir).as_posix()
            out.append((src, f"{_SHARED_DIRNAME}/{rel}"))
        return out

    def _fallback_pack_dir(self, model_id: str) -> Path:
        """Locate a ref's pack dir when ``ref.pack_dir`` is empty.

        Probes built-in first, then user. Returns the first existing dir; if
        neither exists, returns the built-in candidate so the caller records
        a "missing" warning against the canonical path.
        """
        candidates: list[Path] = [(self.pack_root / model_id).resolve()]
        if self._user_pack_root is not None:
            # User pack layout: user_pack_root/<id>/ (no extra "models/" layer)
            candidates.append((self._user_pack_root / model_id).resolve())
        for c in candidates:
            if c.is_dir():
                return c
        return candidates[0]

    def _is_under_any_pack_root(self, path: Path) -> bool:
        """Whether ``path`` lies under built-in ``pack_root`` OR
        ``user_pack_root``. missing anchors are ignored."""
        if self._is_under(path, self.pack_root):
            return True
        if self._user_pack_root is not None and self._is_under(
            path, self._user_pack_root
        ):
            return True
        return False


# ---------------------------------------------------------------------------
# Module-level helpers (pure)
# ---------------------------------------------------------------------------
def _iter_files(base: Path) -> list[Path]:
    """Return every regular file under ``base`` (recursive), caches excluded.

    Symlink-escape guard (plan §5.8): if a file is a symlink whose target
    resolves outside ``base``, it is skipped + logged. ``os.walk`` already
    does not follow symlinked *directories* (``followlinks=False`` default),
    but symlinked *files* still need this explicit check so a malicious /
    misconfigured model dir cannot leak arbitrary files into the package.
    """
    out: list[Path] = []
    try:
        base_real = base.resolve()
    except OSError:
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.casefold() not in _EXCLUDE_DIR_NAMES]
        for fname in files:
            if Path(fname).suffix.casefold() in _EXCLUDE_SUFFIXES:
                continue
            src = Path(root) / fname
            if src.is_symlink():
                try:
                    tgt = src.resolve(strict=True)
                except OSError:
                    logger.warning(
                        "app_project_packager.skip_broken_symlink",
                        path=str(src),
                    )
                    continue
                try:
                    tgt.relative_to(base_real)
                except ValueError:
                    logger.warning(
                        "app_project_packager.skip_escaping_symlink",
                        path=str(src),
                    )
                    continue
            if src.is_file():
                out.append(src)
    return out


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sum_size(paths) -> int:
    return sum(_safe_size(p) for p in paths)


def _pct(copied: int, total: int) -> float:
    return min(100.0, max(0.0, (copied / total) * 100.0)) if total else 0.0


def _unlink_quiet(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _build_manifest(
    *,
    app_id: str,
    definition: AppProjectDefinition,
    model_ids: list[str],
    total_size_bytes: int,
    packaged_at: datetime,
    warnings: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "app_id": app_id,
        "name": definition.name,
        "description": definition.description,
        "packaged_at": packaged_at.isoformat(),
        "models": list(model_ids),
        "include_models": definition.package_include_models,
        "include_outputs": definition.package_include_outputs,
        "total_size_bytes": total_size_bytes,
        "target_platform": (
            f"{_platform_label()} with a QAI ModelBuilder "
            "Python environment. Not a fully self-contained offline package."
        ),
        "warnings": list(warnings),
    }


def _platform_label() -> str:
    """Human-readable target platform for the packaged app manifest."""
    return (
        "Windows on Snapdragon (WoS) ARM64"
        if current_arch() == "arm64"
        else "Windows x64 (Intel/AMD)"
    )


def _cpython_label() -> str:
    """Human-readable CPython arch label for RUNNING.md prerequisites."""
    return "ARM64 CPython" if current_arch() == "arm64" else "x64 CPython"


def _running_md() -> str:
    """Return the RUNNING.md body with arch-specific labels resolved.

    Arch labels are decided at package time from :func:`current_arch`
    (the packaging process's arch) so the doc matches the environment
    the packaged app is intended to run on.
    """
    return (
        "# Running this packaged app\n"
        "\n"
        "This ZIP contains the app's source (backend + frontend), its launch\n"
        "scripts, and — when bundled — the minimal model weight / pack files each\n"
        "model needs.\n"
        "\n"
        "## Requirements: a Python interpreter (+ NPU runtime for on-device inference)\n"
        "\n"
        f"The target machine needs an **ARM64 {_cpython_label()}** matching the\n"
        "one used to build the app (Windows on Snapdragon). `run.bat` first\n"
        "verifies the interpreter is ARM64 (QNN wheels are ARM64-only) and then\n"
        "runs the startup dependency check `backend/ensure_deps.py`, which reads\n"
        "`requirements.txt` and pip-installs anything missing — preferring bundled\n"
        "wheels under `vendor/whl/` and falling back to the configured pip index.\n"
        "\n"
        "The NPU runtime (`qai_appbuilder` + QAIRT) is installed the same way when it is\n"
        "declared in `requirements.txt` and reachable from the configured index or a\n"
        "bundled wheel. If a required package cannot be installed, the launcher stops\n"
        "with a clear message naming what to install — never a mysterious traceback on\n"
        "the first request.\n"
        "\n"
        "## Steps\n"
        "\n"
        f"1. Unzip this package on a {_platform_label()} machine with a\n"
        "   Python interpreter available.\n"
        "3. Run `run.bat`. The first launch checks/installs\n"
        "   dependencies; subsequent launches skip straight through via a sentinel.\n"
        "4. Open the printed local URL in your browser.\n"
        "\n"
        "Bundled model weights are under `models/<model_id>/`; the corresponding\n"
        "pack sources (runner + pack-local modules) are under `pack/<model_id>/`;\n"
        "the framework shared helpers the pack runner imports (`runner_protocol`,\n"
        "`telemetry`, …) are under `shared/`. Optional offline wheels + per-pack\n"
        "runtime data live under `vendor/`.\n"
    )


def _enhanced_requirements(app_dir: Path) -> str:
    """Generate an enhanced ``requirements.txt`` for the packaged zip.

    Reads the original app-level requirements.txt (if it exists) and appends
    a pinned ``qai-appbuilder>=<version>`` line — ensuring that
    ``ensure_deps.py`` on the target machine will auto-upgrade a stale SDK
    before model loading (preventing "newer context binary on old SDK" crashes).

    If ``qai-appbuilder`` is already mentioned in the original file (perhaps
    manually pinned by the app author), the existing line wins (no duplication).
    """
    original = ""
    req_file = app_dir / "requirements.txt"
    if req_file.is_file():
        try:
            original = req_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    # Check if qai-appbuilder already declared.
    if any(
        line.strip().lower().startswith("qai-appbuilder")
        or line.strip().lower().startswith("qai_appbuilder")
        for line in original.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ):
        return original  # respect the app author's explicit pin

    # Detect the qai-appbuilder version available in the packaging environment.
    qai_ver = ""
    try:
        from importlib.metadata import version as _get_ver  # noqa: PLC0415
        qai_ver = _get_ver("qai-appbuilder")
    except Exception:  # noqa: BLE001
        pass

    pin_line = f"qai-appbuilder>={qai_ver}" if qai_ver else "qai-appbuilder"
    # Append after original content (ensure trailing newline).
    base = original.rstrip("\n")
    return f"{base}\n{pin_line}\n" if base else f"{pin_line}\n"


def _run_bat_content(host: str, preferred_port: int) -> str:
    """Generate ``run.bat`` with port-fallback logic.

    Tries the preferred port first; if it's occupied (bind fails), probes
    up to 3 successive ports, then falls back to an OS-assigned free port.
    Opens the browser once the port is confirmed free, then starts uvicorn.
    """
    return (
        "@echo off\n"
        "setlocal EnableExtensions EnableDelayedExpansion\n"
        "cd /d \"%~dp0\"\n"
        "\n"
        "REM \u2500\u2500 Interpreter check: this app needs ARM64 CPython \u2500\u2500\n"
        "REM The QNN wheels (qai_appbuilder) are ARM64-only; a mismatched\n"
        "REM interpreter loads the wrong native .pyd and crashes deep in\n"
        "REM QNNContext with an opaque 0xC0000005. Fail fast here instead.\n"
        "REM NOTE: echo lines below must NOT contain ( ) or the cmd parser\n"
        "REM breaks inside if-blocks; use goto flow rather than parenthesised\n"
        "REM blocks so the diagnostic text can stay human-readable.\n"
        "python -c \"import platform,sys; m=platform.machine().lower(); "
        "sys.exit(0 if ('arm' in m or 'aarch' in m) else 3)\" 2>nul\n"
        "set \"ARCH_RC=%errorlevel%\"\n"
        "if \"%ARCH_RC%\"==\"3\" goto :arch_bad\n"
        "if \"%ARCH_RC%\"==\"9009\" goto :no_python\n"
        "if \"%ARCH_RC%\"==\"1\" goto :no_python\n"
        "goto :arch_ok\n"
        "\n"
        ":arch_bad\n"
        "echo [run.bat] ERROR: the python on PATH is not ARM64 CPython.\n"
        "echo   QNN inference requires ARM64 CPython on Windows on Snapdragon.\n"
        "echo   Activate an ARM64 Python such as the QAI ModelBuilder venv,\n"
        "echo   or put an ARM64 python.exe first on PATH, then re-run.\n"
        "pause\n"
        "exit /b 3\n"
        "\n"
        ":no_python\n"
        "echo [run.bat] ERROR: no usable python found on PATH.\n"
        "echo   Install ARM64 CPython and ensure python resolves to it.\n"
        "pause\n"
        "exit /b 1\n"
        "\n"
        ":arch_ok\n"
        "\n"
        "REM ── Environment setup (mirrors the dev-host env injection) ──\n"
        "REM These vars make the packaged app behave identically to the\n"
        "REM integrated dev-host environment where APP_PROJECT_ROOT /\n"
        "REM PYTHONPATH / model roots are injected by the orchestrator.\n"
        "set \"APP_PROJECT_ROOT=%~dp0\"\n"
        "set \"APP_BUILDER_MODEL_ROOT=%~dp0models\"\n"
        "set \"APP_BUILDER_PACK_ROOT=%~dp0pack\"\n"
        "\n"
        "REM PYTHONPATH: app root (so 'backend.X' resolves), shared helpers\n"
        "REM (qnn_helper, runner_protocol, telemetry, etc.), and backend/utils\n"
        "REM if present. Prepend to any inherited PYTHONPATH.\n"
        "set \"_PP=%~dp0;%~dp0shared;%~dp0backend\\utils\"\n"
        "if defined PYTHONPATH (\n"
        "    set \"PYTHONPATH=%_PP%;%PYTHONPATH%\"\n"
        ") else (\n"
        "    set \"PYTHONPATH=%_PP%\"\n"
        ")\n"
        "set \"_PP=\"\n"
        "\n"
        "REM ── QAIRT SDK PATH injection (ensures matching QNN DLLs) ──────\n"
        "REM The .bin models compiled by the dev-host SDK need the *same* SDK\n"
        "REM version DLLs at runtime.  Priority order:\n"
        "REM   1. QAIRT_SDK_ROOT env (user explicit override)\n"
        "REM   2. Default install dir C:\\Qualcomm\\AIStack\\QAIRT\\*\n"
        "REM If neither exists, the pip package's bundled DLLs are used\n"
        "REM (may fail with 'newer context binary on old SDK').\n"
        "set \"_QNN_LIB=\"\n"
        "if defined QAIRT_SDK_ROOT (\n"
        "    if exist \"%QAIRT_SDK_ROOT%\\lib\\arm64x-windows-msvc\" (\n"
        "        set \"_QNN_LIB=%QAIRT_SDK_ROOT%\\lib\\arm64x-windows-msvc\"\n"
        "    )\n"
        ")\n"
        "if not defined _QNN_LIB (\n"
        "    for /f \"delims=\" %%D in ('dir /b /ad /o-n \"C:\\Qualcomm\\AIStack\\QAIRT\\\" 2^>nul') do (\n"
        "        if not defined _QNN_LIB (\n"
        "            if exist \"C:\\Qualcomm\\AIStack\\QAIRT\\%%D\\lib\\arm64x-windows-msvc\" (\n"
        "                set \"_QNN_LIB=C:\\Qualcomm\\AIStack\\QAIRT\\%%D\\lib\\arm64x-windows-msvc\"\n"
        "            )\n"
        "        )\n"
        "    )\n"
        ")\n"
        "if defined _QNN_LIB (\n"
        "    echo [run.bat] Using QAIRT SDK libs: %_QNN_LIB%\n"
        "    set \"PATH=%_QNN_LIB%;%PATH%\"\n"
        ")\n"
        "set \"_QNN_LIB=\"\n"
        "\n"
        "REM \u2500\u2500 Dependency check (first-run installs missing packages) \u2500\u2500\n"
        "if exist backend\\ensure_deps.py (\n"
        "    python backend\\ensure_deps.py\n"
        "    if errorlevel 1 (\n"
        "        echo [run.bat] Dependency check failed. See errors above.\n"
        "        pause\n"
        "        exit /b 1\n"
        "    )\n"
        ")\n"
        "REM ── Port selection with fallback ──\n"
        f"set \"PREFERRED={preferred_port}\"\n"
        f"set \"HOST={host}\"\n"
        "\n"
        "REM Try the preferred port, then +1, +2, +3, then OS-assigned (0).\n"
        "set /a P1=%PREFERRED%+1\n"
        "set /a P2=%PREFERRED%+2\n"
        "set /a P3=%PREFERRED%+3\n"
        "\n"
        "for %%P in (%PREFERRED% %P1% %P2% %P3%) do (\n"
        "    python -c \"import socket,sys; s=socket.socket(); "
        "s.setsockopt(socket.SOL_SOCKET,0x0004,1); "  # SO_EXCLUSIVEADDRUSE on Windows = 0x0004 (SO_REUSEADDR inverse)
        "s.bind(('%HOST%',%%P)); s.close()\" 2>nul\n"
        "    if not errorlevel 1 (\n"
        "        set \"PORT=%%P\"\n"
        "        goto :port_ok\n"
        "    )\n"
        ")\n"
        "\n"
        "REM All preferred ports occupied — ask the OS for a free one.\n"
        "echo Port %PREFERRED%-%P3% all in use, requesting a free port...\n"
        "for /f \"usebackq\" %%F in (`python -c \"import socket; s=socket.socket(); "
        "s.bind(('%HOST%',0)); print(s.getsockname()[1]); s.close()\"`)"
        " do set \"PORT=%%F\"\n"
        "\n"
        ":port_ok\n"
        "echo.\n"
        "echo ========================================\n"
        "echo   Starting on http://%HOST%:%PORT%/\n"
        "echo ========================================\n"
        "echo.\n"
        "\n"
        "REM Open browser, then start uvicorn (blocks until Ctrl+C).\n"
        "start \"\" http://%HOST%:%PORT%/\n"
        "python -m uvicorn backend.main:app --host %HOST% --port %PORT%\n"
        "\n"
        "echo.\n"
        "echo Uvicorn exited with code %ERRORLEVEL%.\n"
        "pause\n"
        "endlocal\n"
    )
