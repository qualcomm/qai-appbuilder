# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Bridge: PackManifest → RunnerCommandRegistry (PR-303).

Drives :class:`qai.app_builder.infrastructure.command_resolver.InMemoryRunnerCommandRegistry`
from a list of :class:`PackManifest` instances. Resolves the
``manifest.runner.script`` field against the Pack root directory to
produce an absolute :attr:`RunnerSpec.script_path`, and selects the
proper :class:`PythonInterpreterResolver` based on
``manifest.runner.venv`` (legacy compat: ``"system" | "arm64"``).

The bridge is **stateless** — it walks a list of manifests + a root
path on each call and returns/populates a registry. Lifespan can call
it once on startup; PR-306 wraps it into the install workflow.
"""

from __future__ import annotations

import logging
from pathlib import Path

from qai.app_builder.domain.pack_manifest import PackManifest, select_variant
from qai.app_builder.infrastructure.command_resolver import (
    InMemoryRunnerCommandRegistry,
    PythonInterpreterResolver,
    QairtEnvJsonResolver,
    RunnerCommandRegistryPort,
    RunnerSpec,
    SysExecutableResolver,
)

__all__ = ["populate_runner_registry_from_manifests", "select_runner_interpreter"]

logger = logging.getLogger(__name__)


def populate_runner_registry_from_manifests(
    *,
    manifests: tuple[PackManifest, ...],
    pack_root: Path,
    repo_root: Path,
    registry: RunnerCommandRegistryPort | None = None,
    qairt_env_file: Path | None = None,
    extra_pythonpath: tuple[Path, ...] = (),
) -> InMemoryRunnerCommandRegistry:
    """Register a :class:`RunnerSpec` for each manifest.

    Parameters
    ----------
    manifests:
        Decoded manifests (typically from :class:`FileSystemManifestReader`).
    pack_root:
        Directory containing per-model subdirectories
        (``<pack_root>/<model_id>/runner.py``).
    repo_root:
        Repository root used by interpreter resolvers to anchor
        relative venv paths from ``qairt_env.json``.
    registry:
        Optional pre-existing registry to populate. When ``None`` a
        fresh :class:`InMemoryRunnerCommandRegistry` is created.
        Required type: :class:`RunnerCommandRegistryPort`. We narrow
        the return type to :class:`InMemoryRunnerCommandRegistry` so
        callers can chain ``.register(...)`` after this call (the
        Protocol does not declare ``register``).
    qairt_env_file:
        Optional path to ``qairt_env.json``. When provided and the
        manifest declares ``runner.venv == "arm64"``, a
        :class:`QairtEnvJsonResolver` is used. When ``None`` the
        ``"arm64"`` setting falls back to :class:`SysExecutableResolver`.
    extra_pythonpath:
        Directories to *prepend* to every spawn's ``PYTHONPATH``.
        Typically the Pack ``shared/`` helper directory (legacy:
        ``features/app-builder/shared/``; PR-306:
        ``<install>/app-builder/shared/``).

    Returns
    -------
    The populated :class:`InMemoryRunnerCommandRegistry`. Models with
    a missing ``runner.py`` script on disk are logged and skipped — a
    single bad pack must not prevent other packs from registering.
    """
    if not isinstance(pack_root, Path):
        raise TypeError("pack_root must be a Path")
    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be a Path")
    if registry is None:
        registry = InMemoryRunnerCommandRegistry()
    if not isinstance(registry, InMemoryRunnerCommandRegistry):
        # PR-303 only knows how to register on the in-memory impl;
        # the writable-Protocol surface is intentionally narrow.
        raise TypeError(
            "populate_runner_registry_from_manifests requires "
            "InMemoryRunnerCommandRegistry; "
            f"got {type(registry).__name__}"
        )

    interpreter = _select_interpreter(qairt_env_file, repo_root)

    for manifest in manifests:
        spec = _build_runner_spec(
            manifest=manifest,
            pack_root=pack_root,
            extra_pythonpath=extra_pythonpath,
        )
        if spec is None:
            continue
        registry.register(manifest.model_id, spec)
        logger.debug(
            "registered runner spec for %s: %s",
            manifest.model_id, spec.script_path,
        )
    # Even if no manifest registered, return the registry so the caller
    # can still inspect it.
    _ = interpreter  # interpreter is consumed by build_command_resolver,
    # not by the registry itself; the resolver factory wires it later.
    return registry


def _build_runner_spec(
    *,
    manifest: PackManifest,
    pack_root: Path,
    extra_pythonpath: tuple[Path, ...],
) -> RunnerSpec | None:
    pack_dir = pack_root / manifest.model_id
    script_rel = manifest.runner.script or "runner.py"
    script_path = (pack_dir / script_rel).resolve()
    if not script_path.is_file():
        logger.warning(
            "skipping pack %s: runner script %s not found",
            manifest.model_id, script_path,
        )
        return None
    timeout_s: float | None
    if manifest.runner.timeout_ms > 0:
        timeout_s = max(1.0, manifest.runner.timeout_ms / 1000.0)
    else:
        timeout_s = None  # unlimited (legacy semantics)
    return RunnerSpec(
        script_path=script_path,
        cwd=pack_dir,
        extra_pythonpath=extra_pythonpath,
        timeout_s=timeout_s,
        delegate=manifest.runtime.delegate,
    )


def _select_interpreter(
    qairt_env_file: Path | None, repo_root: Path
) -> PythonInterpreterResolver:
    """Pick the interpreter resolver per manifest.runner.venv policy.

    PR-303 keeps it simple: when a qairt_env.json is provided we use the
    QairtEnvJsonResolver (with sys.executable fallback); otherwise we
    use SysExecutableResolver. Per-manifest resolver selection
    (``manifest.runner.venv == "system"`` overriding to sys) is
    intentionally not implemented — production packs declare
    ``"arm64"`` and the fallback already covers dev/test, so the
    extra knob would add complexity without a real-world driver.

    The ``"arm64"`` manifest value is a stable policy token, NOT a hard
    architecture pin: no arch is hardcoded here. QairtEnvJsonResolver
    resolves the concrete runtime venv + QNN subdir from qairt_env.json
    per host arch (``python_runtime_venv`` / ``qairt_runtime_subdir``,
    with legacy arm64 fallback), so an x64 host transparently gets its
    x64 venv without any manifest change.
    """
    if qairt_env_file is None:
        return SysExecutableResolver()
    return QairtEnvJsonResolver(
        env_file=qairt_env_file,
        repo_root=repo_root,
        fallback=SysExecutableResolver(),
    )


# Public alias so DI / use cases can pick the same interpreter the
# registry bridge uses without duplicating the policy. Kept as a thin
# alias rather than renaming ``_select_interpreter`` so existing call
# sites within this module remain stable.
select_runner_interpreter = _select_interpreter
