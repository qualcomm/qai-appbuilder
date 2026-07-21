# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sticky-worker load resolver — ``(Run, model) -> LoadModelRequest``.

The one-shot path uses
:func:`qai.app_builder.infrastructure.command_resolver.build_command_resolver`
to turn ``(Run, AppModelDefinition)`` into a
:class:`qai.platform.process.ports.ProcessExecutionRequest`. The
resident-worker path needs the analogous mapping onto a
:class:`qai.app_builder.infrastructure.sticky_worker.LoadModelRequest`
(the ``op:load`` payload the persistent bootstrap consumes).

Mirrors V1 ``backend/app_builder/runners/sticky_worker.py:215-312``
(``load_model_in_worker``): it resolves ``runnerPath`` / ``packDir`` /
``modelDir`` / ``variantContextBins`` from the manifest + variant, then
sends the ``op:load`` command. Here we build the equivalent value object
from:

* the :class:`RunnerCommandRegistryPort` ``RunnerSpec`` (``script_path``
  → ``runner_path``; ``cwd`` → ``pack_dir`` — identical sources to the
  one-shot resolver, so the two paths stay consistent);
* the manifest provider + :func:`select_variant` (``variant_id`` +
  ``installPath`` → ``model_dir``; ``runtime.context_bins`` →
  ``variant_context_bins``), anchored at ``repo_root`` exactly like
  V1 / :class:`FileSystemWeightsPresence`.

Graceful by design
-------------------
Returns ``None`` when the model has no registered runner spec (the
caller then skips the sticky path and lets the one-shot fallback emit
``no_command``). Manifest / variant resolution is best-effort: a missing
manifest leaves ``model_dir`` empty (the Pack ``runner.py`` self-resolves
it from ``repoRoot`` — see e.g. ``melotts-zh/runner.py:571-579``), so a
sparse test container still produces a usable ``LoadModelRequest``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.pack_manifest import PackManifest, select_variant
from qai.app_builder.domain.run import Run

from .command_resolver import RunnerCommandRegistryPort
from .sticky_worker import LoadModelRequest

__all__ = [
    "StickyLoadResolver",
    "build_load_request_for_model_id",
    "build_sticky_load_resolver",
]

_log = logging.getLogger("qai.app_builder.infrastructure.sticky_load_resolver")


@runtime_checkable
class StickyLoadResolver(Protocol):
    """Callable mapping ``(Run, model)`` onto a :class:`LoadModelRequest`."""

    def __call__(
        self, run: Run, model: AppModelDefinition
    ) -> LoadModelRequest | None:
        ...


# Manifest provider callable shape (``AppModelId | str -> PackManifest | None``).
ManifestProvider = Callable[[Any], "PackManifest | None"]


def build_sticky_load_resolver(
    *,
    registry: RunnerCommandRegistryPort,
    repo_root: Path,
    manifest_provider: ManifestProvider | None = None,
) -> StickyLoadResolver:
    """Wrap a registry + manifest provider into a :class:`StickyLoadResolver`.

    Parameters
    ----------
    registry:
        Source of :class:`RunnerSpec` (``script_path`` / ``cwd``) — the
        same registry the one-shot ``command_resolver`` consults, so the
        sticky and one-shot paths resolve the identical runner script.
    repo_root:
        Repository root used to anchor relative ``installPath`` /
        ``context_bins`` (V1 / :class:`FileSystemWeightsPresence` parity).
    manifest_provider:
        ``AppModelId | str -> PackManifest | None``. Used to resolve the
        selected variant + its weights/context-bin paths. ``None`` (or a
        provider returning ``None``) leaves ``model_dir`` empty and the
        Pack runner self-resolves it from ``repoRoot``.
    """

    def _resolve(
        run: Run, model: AppModelDefinition
    ) -> LoadModelRequest | None:
        spec = registry.get(model, run)
        if spec is None:
            return None

        model_id = str(model.id)
        variant_id = _extract_variant_id(run.inputs)

        model_dir: Path = spec.cwd  # sensible default = pack dir
        context_bins: tuple[Path, ...] = ()

        manifest = _safe_manifest(manifest_provider, model.id)
        if manifest is not None:
            try:
                variant = select_variant(manifest, variant_id)
            except ValueError:
                # Invalid variant id — fall through with the requested id
                # so the worker surfaces the same error V1 would; the Pack
                # runner validates again on its side.
                variant = None
            install_path, bins = _variant_paths(manifest, variant)
            if install_path:
                model_dir = _anchor(install_path, repo_root)
            context_bins = tuple(
                _anchor(b, repo_root) for b in bins if b
            )

        try:
            return LoadModelRequest(
                model_id=model_id,
                variant_id=variant_id,
                runner_path=spec.script_path,
                pack_dir=spec.cwd,
                model_dir=model_dir,
                repo_root=repo_root,
                variant_context_bins=context_bins,
            )
        except ValueError:
            _log.warning(
                "failed to build LoadModelRequest for model %s", model_id,
                exc_info=True,
            )
            return None

    return _resolve


def build_load_request_for_model_id(
    *,
    registry: RunnerCommandRegistryPort,
    repo_root: Path,
    model_id: str,
    variant_id: str | None,
    manifest_provider: ManifestProvider | None = None,
) -> LoadModelRequest | None:
    """Build a :class:`LoadModelRequest` for a bare ``model_id`` (no Run).

    Used by the lifespan voice warm-up, which loads a model into the
    resident worker before any :class:`Run` exists (V1
    ``backend/main.py:680-706`` warm-up calls ``get_or_create_worker`` with
    just ``model_id`` / ``variant_id`` / ``manifest``). Resolution mirrors
    :func:`build_sticky_load_resolver` but keys off the id directly.

    The in-memory registry indexes by ``str(model.id)`` and ignores
    ``run``, so we look up the :class:`RunnerSpec` via a tiny shim exposing
    ``.id`` and a ``None`` run. Returns ``None`` when the model has no
    registered runner spec.
    """

    class _ModelIdShim:
        __slots__ = ("id",)

        def __init__(self, value: str) -> None:
            self.id = value

    spec = registry.get(_ModelIdShim(model_id), None)  # type: ignore[arg-type]
    if spec is None:
        return None

    model_dir: Path = spec.cwd
    context_bins: tuple[Path, ...] = ()
    manifest = _safe_manifest(manifest_provider, model_id)
    if manifest is not None:
        try:
            variant = select_variant(manifest, variant_id)
        except ValueError:
            variant = None
        install_path, bins = _variant_paths(manifest, variant)
        if install_path:
            model_dir = _anchor(install_path, repo_root)
        context_bins = tuple(_anchor(b, repo_root) for b in bins if b)

    try:
        return LoadModelRequest(
            model_id=model_id,
            variant_id=variant_id,
            runner_path=spec.script_path,
            pack_dir=spec.cwd,
            model_dir=model_dir,
            repo_root=repo_root,
            variant_context_bins=context_bins,
        )
    except ValueError:
        _log.warning(
            "failed to build warm-up LoadModelRequest for %s", model_id,
            exc_info=True,
        )
        return None


def _extract_variant_id(run_inputs: Mapping[str, Any]) -> str | None:
    """Read the optional ``variant_id`` bundled into :attr:`Run.inputs`.

    Parity with ``command_resolver._split_run_inputs`` /
    ``run_app._extract_variant_id``.
    """
    value = (run_inputs or {}).get("variant_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_manifest(
    provider: ManifestProvider | None, model_id: Any
) -> PackManifest | None:
    if provider is None:
        return None
    try:
        return provider(model_id)
    except Exception:  # noqa: BLE001 — manifest read must never break a run
        _log.debug("manifest provider raised for %s", model_id, exc_info=True)
        return None


def _variant_paths(
    manifest: PackManifest, variant: Any
) -> tuple[str, tuple[str, ...]]:
    """Return ``(install_path, context_bins)`` for the selected variant.

    Falls back to the top-level manifest ``assets`` / ``runtime`` when the
    pack is single-variant (``select_variant`` returned ``None``), mirroring
    V1 ``synthesize_default_variant`` semantics for the load paths.
    """
    if variant is not None:
        install_path = getattr(getattr(variant, "assets", None), "install_path", "")
        runtime = getattr(variant, "runtime", None)
        context_bins = tuple(getattr(runtime, "context_bins", ()) or ())
        return install_path or "", context_bins
    # Single-variant pack → use the top-level manifest fields.
    install_path = manifest.assets.install_path
    context_bins = tuple(manifest.runtime.context_bins or ())
    return install_path or "", context_bins


def _anchor(rel_or_abs: str, repo_root: Path) -> Path:
    """Anchor a possibly-relative path at ``repo_root`` (V1 parity)."""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return p
