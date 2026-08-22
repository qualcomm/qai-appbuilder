# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``model_builder`` bounded context (S9 close).

Materialises :class:`qai.model_builder.application` use cases against
real adapters:

* :class:`WosAiWorkspaceReader` for the workspace probe;
* :class:`RuleAndShapeTaxonomyClassifier` for ``model_name -> task``;
* :class:`QaiPackExporter` for the actual ``app_pack/`` emission;
* :class:`QaiPackValidator` for post-emit structural validation;
* :class:`FileSystemWorkspaceInitializer` for the ``init``-style
  bootstrap path.

The HTTP route ``POST /api/app-builder/import/auto-export`` reaches
this graph via :class:`apps.api._app_builder_model_builder_bridge.AppBuilderModelBuilderBridge`
so the cross-context boundary stays single-direction
(``apps.api -> qai.model_builder``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qai.model_builder.adapters import (
    FileSystemWorkspaceInitializer,
    QaiPackExporter,
    QaiPackValidator,
    RuleAndShapeTaxonomyClassifier,
    WosAiWorkspaceReader,
)
from qai.model_builder.application.ports import (
    PackExporterPort,
    PackValidatorPort,
    TaxonomyClassifierPort,
    WorkspaceReaderPort,
)
from qai.model_builder.application.use_cases.export_pack import (
    ExportPackUseCase,
)
from qai.model_builder.application.use_cases.init_workspace import (
    InitWorkspaceUseCase,
)
from qai.model_builder.application.use_cases.validate_pack import (
    ValidatePackUseCase,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = ["ModelBuilderServices", "build_model_builder_services"]


# Default WoS_AI root used by the workspace reader. Tests that need
# isolation pass a ``tmp_path`` here via ``Container.model_builder
# = build_model_builder_services(c, wos_ai_root=tmp_path)``. In
# production the root is resolved from the workspace config (forge.config
# override → platform Settings default) by ``resolve_workspace_root``;
# this literal is only the last-resort fallback when that resolution
# yields nothing usable.
_DEFAULT_WOS_AI_ROOT = Path("C:/WoS_AI")

# Pack ``shared/`` helper directory (``qnn_helper.py`` / ``io_validator.py``),
# relative to the repo root. Same constant the App Builder runtime DI uses to
# prepend the runners' PYTHONPATH (``_app_builder_di._DEFAULT_SHARED_REL``).
# The export-time I/O contract probe needs the very same modules importable so
# it can load the live ``.bin`` and record its REAL input/output shapes into
# ``manifest.io_contract`` — exactly what V1's exporter did by inserting
# ``features/app-builder/shared`` onto ``sys.path``.
_DEFAULT_SHARED_REL = ("factory", "chat_features", "app-builder", "shared")


def _resolve_smoke_test_child_launch(
    container: "Container",
) -> tuple[tuple[str, ...] | None, dict[str, str] | None]:
    """Resolve ``(interpreter_argv, env)`` for the one-shot smoke-test child.

    Mirrors ``apps.api.lifespan._spawn_sticky_worker`` (lifespan.py:1740-1764):
    the Pack import smoke test now runs the QNN native load + zero-tensor
    inference in a throwaway child process, and that child must use the SAME
    ARM64 venv interpreter + QAIRT SDK env / ``PATH`` extras the sticky worker
    and one-shot Pack runner use, so ``qai_appbuilder`` and the QNN runtime DLLs
    load identically.

    Returns ``(None, None)`` (probe then falls back to ``sys.executable`` +
    ``os.environ``) on any resolution failure, so a diagnostics glitch never
    blocks export wiring — dev / non-NPU hosts then simply fail the child's
    ``qai_appbuilder`` import, surfacing the same ``MissingQaiAppBuilderError``
    as before.
    """
    try:
        from qai.app_builder.infrastructure.app_manifest import (
            select_runner_interpreter,
        )
    except Exception:  # noqa: BLE001 — non-app_builder builds: use defaults
        return None, None

    repo_root = getattr(container, "repo_root", None)
    if not isinstance(repo_root, Path):
        return None, None

    try:
        interpreter = select_runner_interpreter(
            qairt_env_file=getattr(container, "qairt_env_file", None),
            repo_root=repo_root,
        )
        python_exe = interpreter.resolve()

        base_env = dict(os.environ)
        extra_env_fn = getattr(interpreter, "extra_env", None)
        if callable(extra_env_fn):
            for _k, _v in extra_env_fn().items():
                base_env[str(_k)] = str(_v)
        path_segments_fn = getattr(interpreter, "path_segments", None)
        if callable(path_segments_fn):
            segments = path_segments_fn()
            if segments:
                prefix = os.pathsep.join(str(s) for s in segments)
                existing = base_env.get("PATH", "")
                base_env["PATH"] = (
                    prefix + (os.pathsep + existing if existing else "")
                )
        return (str(python_exe),), base_env
    except Exception:  # noqa: BLE001 — never break DI on resolver failure
        return None, None


def _resolve_app_builder_shared_dir(container: "Container") -> Path | None:
    """Resolve the App Builder Pack ``shared/`` helper directory.

    Mirrors :func:`apps.api._app_builder_di._pack_shared_pythonpath` so the
    export-time I/O contract probe finds ``qnn_helper`` / ``io_validator`` at
    the *same* location the runtime runners import them from:

    1. ``container.app_builder_shared_dir`` when explicitly injected
       (lifespan hook / test override) and it points at a real dir.
    2. ``<repo_root>/factory/chat_features/app-builder/shared`` — the bundled helpers
       shipped with the v2.7 install layout.

    Returns ``None`` when neither exists; the probe then surfaces a clear
    :class:`MissingQaiAppBuilderError` instead of silently writing a
    placeholder contract.
    """
    injected = getattr(container, "app_builder_shared_dir", None)
    if isinstance(injected, Path) and injected.is_dir():
        return injected
    repo_root = getattr(container, "repo_root", None)
    if isinstance(repo_root, Path):
        candidate = repo_root.joinpath(*_DEFAULT_SHARED_REL)
        if candidate.is_dir():
            return candidate
    return None


@dataclass(slots=True)
class ModelBuilderServices:
    """Application services / ports for the ``model_builder`` namespace.

    Field-name lock (v2.7 §3.1): every field here is preserved
    verbatim across future revisions; new fields tail-append only.
    """

    workspace_reader: WorkspaceReaderPort
    taxonomy_classifier: TaxonomyClassifierPort
    pack_exporter: PackExporterPort
    pack_validator: PackValidatorPort
    export_pack_use_case: ExportPackUseCase
    validate_pack_use_case: ValidatePackUseCase
    init_workspace_use_case: InitWorkspaceUseCase


def build_model_builder_services(
    container: "Container",
    *,
    wos_ai_root: Path | None = None,
    qai_appbuilder_shared_dir: Path | None = None,
    skip_smoke_test: bool | None = None,
) -> ModelBuilderServices:
    """Wire ``container.model_builder`` against real adapters.

    Optional knobs:

    * ``wos_ai_root`` — root directory for the workspace reader's
      path-traversal guard. When ``None`` (production) it is resolved
      from the workspace config via :func:`resolve_workspace_root`
      (forge.config override → platform Settings default → ``C:/WoS_AI``).
      Tests pass an explicit ``tmp_path`` to bypass that resolution.
    * ``qai_appbuilder_shared_dir`` — optional path that bundles
      ``qnn_helper.py`` / ``io_validator.py`` (the App Builder shared
      runner helpers). When provided the I/O contract probe inserts
      it into ``sys.path`` lazily.
    * ``skip_smoke_test`` — when ``True``, the exporter writes a
      placeholder ``io_contract`` instead of loading the ``.bin``.
      Default ``False`` to preserve the legacy hard-abort policy on
      missing ``qai_appbuilder``; set ``True`` only on hosts that
      author Packs from remote ``.bin`` files without the runtime
      installed.

    Container collaborators consumed: ``container`` is read via
    :func:`resolve_workspace_root` to resolve the workspace root when
    ``wos_ai_root`` is not supplied (single source of truth shared with
    the chat / security / frontend consumers).
    """
    if wos_ai_root is not None:
        root = wos_ai_root
    else:
        # Resolve the configured workspace root (forge.config override →
        # platform Settings default). Best-effort: any failure falls back
        # to the legacy literal so wiring never breaks startup.
        try:
            from ._workspace_resolver import resolve_workspace_root

            root = Path(resolve_workspace_root(container))
        except Exception:  # noqa: BLE001 — never break DI on config read
            root = _DEFAULT_WOS_AI_ROOT
    skip_smoke = bool(skip_smoke_test) if skip_smoke_test is not None else False

    # Resolve the Pack ``shared/`` helpers so the export-time I/O contract
    # probe can ``import qnn_helper`` / ``io_validator`` and record the live
    # model's REAL shapes into ``manifest.io_contract``. When the caller did
    # not pin one, fall back to ``<repo_root>/factory/chat_features/app-builder/shared``
    # (same dir the runtime runners use). Without this the probe's import
    # fails, the non-strict exporter writes a placeholder ``io_contract``
    # (empty ``inputs``/``outputs``), and App Builder later rejects the Pack
    # with ``manifest.io_contract.inputs.shape != live getInputShapes()``.
    shared_dir = (
        qai_appbuilder_shared_dir
        if qai_appbuilder_shared_dir is not None
        else _resolve_app_builder_shared_dir(container)
    )

    workspace_reader = WosAiWorkspaceReader(wos_ai_root=root)
    taxonomy_classifier = RuleAndShapeTaxonomyClassifier()

    # Resolve the ARM64 venv interpreter + QAIRT SDK env/PATH extras for the
    # one-shot smoke-test child process (same resolution the sticky worker uses,
    # lifespan.py:1740-1764). The QNN native load + zero-tensor inference now
    # runs in that throwaway child so its native fd output / crashes stay out of
    # this service process. ``(None, None)`` falls back to sys.executable +
    # os.environ (dev/non-NPU), preserving the prior MissingQaiAppBuilderError
    # behaviour on hosts without the runtime.
    smoke_interpreter_argv, smoke_env = _resolve_smoke_test_child_launch(container)

    pack_exporter = QaiPackExporter(
        classifier=taxonomy_classifier,
        qai_appbuilder_shared_dir=shared_dir,
        skip_smoke_test=skip_smoke,
        # Default to non-strict so a missing qai_appbuilder runtime
        # does not turn every export into a 5xx; the exporter logs
        # the failure into ``result.errors`` and falls back to a
        # placeholder I/O contract. Strict mode (legacy hard-abort)
        # is opt-in per deployment via a future Settings field.
        require_qai_appbuilder=False,
        smoke_test_interpreter_argv=smoke_interpreter_argv,
        smoke_test_env=smoke_env,
    )
    pack_validator = QaiPackValidator()

    workspace_initializer = FileSystemWorkspaceInitializer(wos_ai_root=root)

    export_pack_use_case = ExportPackUseCase(
        workspace_reader=workspace_reader,
        pack_exporter=pack_exporter,
        pack_validator=pack_validator,
    )
    validate_pack_use_case = ValidatePackUseCase(
        pack_validator=pack_validator,
    )
    init_workspace_use_case = InitWorkspaceUseCase(
        workspace_initializer=workspace_initializer,
    )

    return ModelBuilderServices(
        workspace_reader=workspace_reader,
        taxonomy_classifier=taxonomy_classifier,
        pack_exporter=pack_exporter,
        pack_validator=pack_validator,
        export_pack_use_case=export_pack_use_case,
        validate_pack_use_case=validate_pack_use_case,
        init_workspace_use_case=init_workspace_use_case,
    )
