# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model install-status detection (V1 ``registry._detect_status`` parity).

Pure-domain port of the legacy
``backend/app_builder/registry.py:_detect_status`` /
``_detect_variant_status`` weight-presence probe. Given a decoded
:class:`~qai.app_builder.domain.pack_manifest.PackManifest`, this module
maps a manifest to the V1-parity status dot (``Ready`` /
``NotInstalled`` / ``Error``) so the App Builder gallery can render it.

The legacy backend computed this inline in the registry while scanning
packs, doing the filesystem probe (``Path.exists()`` / ``.is_dir()`` /
``.iterdir()`` / ``.resolve()``) right there. V2 keeps the *decision
logic* here as a pure function but **injects** the filesystem probe via
a :class:`WeightsProbe` (the adapter — implementing
``qai.app_builder.application.ports.WeightsPresencePort`` — lives in
``infrastructure/`` and is wired by ``apps/api/_app_builder_di.py``).
This module therefore performs **no IO at all**: no ``pathlib`` probe
calls, no ``os`` access, only pure mapping from "probe results" to a
status enum. That satisfies the ``layered-app_builder`` + domain-purity
contracts (including the runtime-purity scanner — no ``.exists()`` etc.
remain in domain).

Status semantics (verbatim from V1 ``_detect_status``):

* ``Ready``        — weights present (default variant installed, or the
  legacy single-pack ``assets.installPath`` / ``weights/`` dir exists).
* ``NotInstalled`` — weights absent (installPath missing on disk).
* ``Error``        — an :class:`OSError` was raised while probing
  (permission / IO error stat'ing the path). The probe surfaces this by
  raising :class:`OSError`; this module catches it and maps to ``Error``.

``variant_status`` mirrors V1 ``_detect_variant_status``: a per-variant
``[(id, status), ...]`` list (only for multi-variant manifests; ``None``
for legacy single-variant packs).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.app_builder.domain.pack_manifest import AssetsSpec, PackManifest, VariantSpec

__all__ = [
    "ModelStatus",
    "VariantStatus",
    "WeightsProbe",
    "detect_status",
    "detect_variant_status",
]

# Status string constants — exactly the tokens V1 emitted on the wire so
# the frontend status-dot mapping (ModelCard.vue) keeps working unchanged.
ModelStatus = str  # one of "Ready" | "NotInstalled" | "Error"

_READY = "Ready"
_NOT_INSTALLED = "NotInstalled"
_ERROR = "Error"


@runtime_checkable
class WeightsProbe(Protocol):
    """Structural type for the injected filesystem weight-presence probe.

    The domain only describes *what* it needs to know ("is this install
    path present?", "does this pack's ``weights/`` dir have content?")
    — never *how* the disk is read. The concrete probe is an adapter
    implementing :class:`qai.app_builder.application.ports.WeightsPresencePort`
    (same method shapes); it lives in ``infrastructure/`` and is wired by
    the DI root. Keeping the structural type here (rather than importing
    the application port) preserves the ``domain ⇍ application`` layering
    while still letting :func:`detect_status` delegate all IO.

    Implementations MAY raise :class:`OSError` on a probe failure
    (permission / IO error); the domain catches it and maps to ``Error``.
    """

    def install_path_present(self, install_path: str) -> bool:
        """Whether ``install_path``'s weights exist on disk (V1 ``.exists()``).

        ``install_path`` is the manifest's (possibly repo-root-relative)
        ``assets.installPath``; the adapter is responsible for resolving
        it against the repo root before stat'ing (V1
        ``(_repo_root / p).resolve().exists()``).
        """
        ...

    def pack_weights_dir_is_present_but_empty(self, pack_id: str) -> bool:
        """Whether ``<pack_root>/<pack_id>/weights/`` exists yet is empty.

        Mirrors the V1 legacy-pack fallback predicate exactly:
        ``weights_dir.is_dir() and not any(weights_dir.iterdir())``.
        Returns ``True`` **only** when the dir is present *and* empty —
        the sole condition that downgrades a legacy pack to
        ``NotInstalled``. An *absent* dir or a *non-empty* dir both yield
        ``False`` (matching V1, where an absent dir leaves the prior
        Ready/NotInstalled decision intact). The adapter owns the
        ``pack_root`` join.
        """
        ...


class VariantStatus:
    """One ``(variant_id, status)`` row of :func:`detect_variant_status`.

    A plain immutable value holder (kept lightweight rather than a full
    ``@dataclass`` to avoid slots/eq overhead for a 2-field tuple-like
    record). ``status`` is one of the :data:`ModelStatus` tokens.
    """

    __slots__ = ("id", "status")

    def __init__(self, *, id: str, status: str) -> None:  # noqa: A002
        self.id = id
        self.status = status

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, VariantStatus)
            and other.id == self.id
            and other.status == self.status
        )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"VariantStatus(id={self.id!r}, status={self.status!r})"


def _variant_install_present(variant: VariantSpec, *, probe: WeightsProbe) -> bool:
    """Whether a variant's weights file is present (V1 parity).

    Pure decision: a variant with no ``installPath`` is never present;
    otherwise delegate the disk check to the injected ``probe``.
    """
    install_path = variant.assets.install_path
    if not install_path:
        return False
    return probe.install_path_present(install_path)


def detect_status(
    manifest: PackManifest,
    *,
    probe: WeightsProbe,
) -> ModelStatus:
    """Map ``manifest``'s injected probe results to an install status.

    Port of V1 ``registry._detect_status`` (decision logic only — the
    filesystem reads are delegated to ``probe``):

    * Multi-variant manifest → ``Ready`` iff the default variant's
      installPath is present (other variants surfaced via
      :func:`detect_variant_status`). ``NotInstalled`` when there is no
      default or the default is missing.
    * Legacy single-variant manifest → check ``assets.installPath`` (when
      set) and the fallback ``<pack_dir>/weights/`` directory.
    * Any :class:`OSError` raised by the probe → ``Error``.
    """
    try:
        if manifest.variants:
            default = _select_default_variant(manifest)
            if default is None:
                return _NOT_INSTALLED
            if not _variant_install_present(default, probe=probe):
                return _NOT_INSTALLED
            # Default present → Ready (even if other variants are missing;
            # the gaps are reported per-variant by detect_variant_status).
            return _READY

        # Legacy manifest (no variants[]) — mirror V1 exactly.
        assets: AssetsSpec = manifest.assets
        install_path = assets.install_path
        if install_path:
            if not probe.install_path_present(install_path):
                return _NOT_INSTALLED
        # Fallback: a *present but empty* ``<pack_dir>/weights/`` dir is the
        # only condition that downgrades to NotInstalled (V1
        # ``is_dir() and not any(iterdir())``). An absent or non-empty dir
        # leaves the prior decision intact → Ready.
        if probe.pack_weights_dir_is_present_but_empty(manifest.model_id):
            return _NOT_INSTALLED
        return _READY
    except OSError:
        return _ERROR


def detect_variant_status(
    manifest: PackManifest,
    *,
    probe: WeightsProbe,
) -> tuple[VariantStatus, ...] | None:
    """Per-variant install status list (V1 ``_detect_variant_status``).

    Returns ``None`` for legacy single-variant manifests (the field is
    omitted on the wire, matching V1). For multi-variant manifests returns
    ``(VariantStatus(id, status), ...)`` where ``status`` is ``Ready`` /
    ``NotInstalled`` / ``Error``. All disk reads are delegated to ``probe``.
    """
    if not manifest.variants:
        return None
    out: list[VariantStatus] = []
    for v in manifest.variants:
        try:
            status = (
                _READY
                if _variant_install_present(v, probe=probe)
                else _NOT_INSTALLED
            )
        except OSError:
            status = _ERROR
        out.append(VariantStatus(id=v.id, status=status))
    return tuple(out)


def _select_default_variant(manifest: PackManifest) -> VariantSpec | None:
    """Return the ``default=true`` variant, else the first, else ``None``.

    Mirrors V1 ``_variants.get_default_variant`` semantics (used only by
    :func:`detect_status` to decide overall Ready/NotInstalled).
    """
    if not manifest.variants:
        return None
    for v in manifest.variants:
        if v.default:
            return v
    return manifest.variants[0]
