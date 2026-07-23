# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Manifest schema v1 value objects for App Builder Pack metadata (PR-303).

These VOs faithfully model the on-disk ``manifest.json`` schema used by
the legacy ``features/app-builder/models/<id>/manifest.json`` and the
new release path ``factory/chat_features/app-builder/models/<id>/manifest.json``
(after PR-306). They are pure domain types — no IO, no framework
dependencies — so the application layer can manipulate manifests
without touching the filesystem.

Schema reference: ``docs/30-ui-ux/multi-variant-pack-contract.md`` §1
+ ``docs/30-ui-ux/app-builder-model-pack-architecture.md`` §C★.

Cardinality
-----------

A manifest describes **one** AppModel. The manifest's ``modelId`` MUST
equal the parent :class:`AppModelDefinition`'s ``id``. The manifest
carries:

* ``schema_version`` — currently always ``1``;
* metadata (``displayName``, ``description``, ``vendor``, ``version``);
* :class:`RuntimePack` — backend / delegate / quantization / device
  set / context bins;
* :class:`PackInputSchema` / :class:`PackOutputSchema` — UI-driven
  input / output kind + constraints;
* :class:`PackParam` list — Pack-specific parameter knobs;
* :class:`MetricsSpec` — declared latency / memory hints;
* :class:`AssetsSpec` — weights URL / checksum / size / install path;
* :class:`PackRunnerSpec` — script / venv / requirements / timeout;
* :class:`Capabilities` — feature flags (streaming / batch / cancel);
* :class:`VariantSpec` list — multi-variant pack contract;
* :class:`SkillRef` — optional SKILL.md reference.

Multi-variant
-------------

A manifest with ``variants[]`` of length 0 means "single implicit
default variant" (legacy compat for old packs). Length ≥ 1 means
"explicit variants"; the variant flagged ``default=true`` is selected
when no ``variantId`` is provided. :func:`select_variant` is the
canonical resolver.

Field naming
------------

Manifest JSON uses ``camelCase`` (web tradition) while Python VOs use
``snake_case``. The :mod:`qai.app_builder.infrastructure.app_manifest`
adapter does the field-name translation; domain VOs never see camelCase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "AssetsSpec",
    "Capabilities",
    "InputKind",
    "MetricsSpec",
    "OutputKind",
    "PackInputSchema",
    "PackManifest",
    "PackOutputSchema",
    "PackParam",
    "PackRunnerSpec",
    "RuntimePack",
    "SkillRef",
    "VariantSpec",
    "select_variant",
]


# ---------------------------------------------------------------------------
# Discriminator literals
# ---------------------------------------------------------------------------
InputKind = Literal["audio", "image", "text", "json", "video"]
"""Allowed values for :attr:`PackInputSchema.kind`.

Matches SSOT ``app-builder-model-pack-architecture.md`` §F.
"""

OutputKind = Literal["audio", "image", "text", "json", "video"]
"""Allowed values for :attr:`PackOutputSchema.kind`.

Matches SSOT §G. Whisper/Zipformer use ``json`` (rich transcript
structure); MeloTTS/Real-ESRGAN use ``audio``/``image``.
"""


# ---------------------------------------------------------------------------
# Runtime / runner / capabilities
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimePack:
    """Runtime configuration declared by ``manifest.runtime``.

    Contains the backend / delegate / quantization combo and the set of
    NPU context binaries the Pack needs at load time.
    """

    backend: str = ""
    delegate: str = ""
    quantization: str = ""
    model_size_mb: int = 0
    context_bins: tuple[str, ...] = field(default_factory=tuple)
    supported_devices: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name, value in (
            ("backend", self.backend),
            ("delegate", self.delegate),
            ("quantization", self.quantization),
        ):
            if not isinstance(value, str):
                raise ValueError(f"RuntimePack.{name} must be str")
        if not isinstance(self.model_size_mb, int) or isinstance(
            self.model_size_mb, bool
        ):
            raise ValueError("RuntimePack.model_size_mb must be int")
        if self.model_size_mb < 0:
            raise ValueError(
                f"RuntimePack.model_size_mb must be >= 0, got {self.model_size_mb}"
            )
        if not isinstance(self.context_bins, tuple):
            raise ValueError("RuntimePack.context_bins must be a tuple of str")
        for i, b in enumerate(self.context_bins):
            if not isinstance(b, str) or not b:
                raise ValueError(
                    f"RuntimePack.context_bins[{i}] must be non-empty str"
                )
        if not isinstance(self.supported_devices, tuple):
            raise ValueError(
                "RuntimePack.supported_devices must be a tuple of str"
            )
        for i, d in enumerate(self.supported_devices):
            if not isinstance(d, str) or not d:
                raise ValueError(
                    f"RuntimePack.supported_devices[{i}] must be non-empty str"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class PackRunnerSpec:
    """``manifest.runner`` configuration.

    Distinct from :class:`qai.app_builder.infrastructure.command_resolver.RunnerSpec`
    which is the runtime command (argv / cwd / env). This VO is the
    *declarative* manifest field.
    """

    type: str = "python-script"
    script: str = "runner.py"
    venv: str = "arm64"
    requirements: str = ""
    timeout_ms: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("type", self.type),
            ("script", self.script),
            ("venv", self.venv),
            ("requirements", self.requirements),
        ):
            if not isinstance(value, str):
                raise ValueError(f"PackRunnerSpec.{name} must be str")
        if not isinstance(self.timeout_ms, int) or isinstance(
            self.timeout_ms, bool
        ):
            raise ValueError("PackRunnerSpec.timeout_ms must be int")
        if self.timeout_ms < 0:
            raise ValueError(
                f"PackRunnerSpec.timeout_ms must be >= 0, got {self.timeout_ms}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Capabilities:
    """``manifest.capabilities`` flag set."""

    streaming: bool = False
    batch: bool = False
    benchmark: bool = False
    cancel: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("streaming", self.streaming),
            ("batch", self.batch),
            ("benchmark", self.benchmark),
            ("cancel", self.cancel),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"Capabilities.{name} must be bool")


# ---------------------------------------------------------------------------
# Schema (input / output)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PackInputSchema:
    """``manifest.inputSchema``.

    SSOT §F: ``kind`` drives which input component the UI renders.
    ``constraints`` is an opaque mapping (different fields per kind:
    ``sampleRate`` / ``channels`` / ``maxSec`` / ``formats`` /
    ``maxMB`` / ...).
    """

    kind: InputKind
    constraints: tuple[tuple[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in ("audio", "image", "text", "json", "video"):
            raise ValueError(
                f"PackInputSchema.kind must be a valid InputKind, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.constraints, tuple):
            raise ValueError(
                "PackInputSchema.constraints must be a tuple of (key, value) pairs"
            )
        for i, item in enumerate(self.constraints):
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError(
                    f"PackInputSchema.constraints[{i}] must be a 2-tuple"
                )
            if not isinstance(item[0], str):
                raise ValueError(
                    f"PackInputSchema.constraints[{i}][0] must be str"
                )

    @property
    def constraints_dict(self) -> dict[str, object]:
        """Materialise constraints as a plain dict (transport view)."""
        return dict(self.constraints)

    def constraint(self, key: str, default: object = None) -> object:
        """Lookup a single constraint value by key."""
        for k, v in self.constraints:
            if k == key:
                return v
        return default

    @property
    def formats(self) -> tuple[str, ...]:
        """Convenience: ``constraints.formats`` as a tuple of str.

        Returns ``()`` when the schema does not declare ``formats``.
        Many UI components (image dropzone / audio uploader) need only
        this, so we promote it to a typed accessor.
        """
        value = self.constraint("formats")
        if isinstance(value, (list, tuple)):
            return tuple(str(x) for x in value)
        return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PackOutputSchema:
    """``manifest.outputSchema``.

    Mirror of :class:`PackInputSchema` for the runner's output shape;
    SSOT §G drives which output viewer the UI renders.
    """

    kind: OutputKind
    constraints: tuple[tuple[str, object], ...] = field(default_factory=tuple)
    json_schema: tuple[tuple[str, object], ...] | None = None
    """Optional JSON Schema (kind=json) preserved as a key-value tuple.

    Stored as tuple-of-tuples so the VO stays hashable. The application
    layer materialises it back to a dict when surfacing to clients.
    """

    def __post_init__(self) -> None:
        if self.kind not in ("audio", "image", "text", "json", "video"):
            raise ValueError(
                f"PackOutputSchema.kind must be a valid OutputKind, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.constraints, tuple):
            raise ValueError(
                "PackOutputSchema.constraints must be a tuple of (key, value) pairs"
            )
        if self.json_schema is not None and not isinstance(
            self.json_schema, tuple
        ):
            raise ValueError(
                "PackOutputSchema.json_schema must be tuple or None"
            )

    @property
    def constraints_dict(self) -> dict[str, object]:
        return dict(self.constraints)

    @property
    def json_schema_dict(self) -> dict[str, object] | None:
        return dict(self.json_schema) if self.json_schema is not None else None

    @property
    def formats(self) -> tuple[str, ...]:
        value = next(
            (v for k, v in self.constraints if k == "formats"), None
        )
        if isinstance(value, (list, tuple)):
            return tuple(str(x) for x in value)
        return ()


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PackParam:
    """One row of ``manifest.params`` — a Pack-specific knob.

    The Pack runner reads this from ``request.params`` JSON; the UI
    renders it as a form control matching :attr:`type`.
    """

    name: str
    label: str = ""
    type: str = "string"
    default: object = None
    # V1 ParamSchema parity: numeric range hints, select options, and the
    # basic/advanced grouping flag, so the UI can render sliders / dropdowns /
    # an Advanced section (tail-appended optional fields).
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[object, ...] | None = None
    advanced: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("PackParam.name must be non-empty str")
        if not isinstance(self.label, str):
            raise ValueError("PackParam.label must be str")
        if not isinstance(self.type, str):
            raise ValueError("PackParam.type must be str")
        for fname, fval in (("min", self.min), ("max", self.max), ("step", self.step)):
            if fval is not None and (
                not isinstance(fval, (int, float)) or isinstance(fval, bool)
            ):
                raise ValueError(f"PackParam.{fname} must be number or None")
        if self.options is not None and not isinstance(self.options, tuple):
            raise ValueError("PackParam.options must be a tuple or None")
        if not isinstance(self.advanced, bool):
            raise ValueError("PackParam.advanced must be bool")


# ---------------------------------------------------------------------------
# Examples (V1 manifest.examples — preset inputs/params for one-click apply)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PackExample:
    """One row of ``manifest.examples`` — a preset input / params bundle.

    V1 parity: when the user clicks an "Apply example" button in the model
    info drawer, the manifest's preset ``inputs`` (e.g. a sample audio path)
    and ``params_override`` (overrides on top of param defaults) get pushed
    into the workbench so the user can run the model with one click.

    The ``inputs`` and ``params_override`` are intentionally typed as opaque
    JSON dicts (``dict[str, object]``) — the manifest schema does not
    constrain their shape (each model decides what its inputs look like),
    and the workbench just forwards them to the run.
    """

    name: str = ""
    license: str = ""
    inputs: dict[str, object] = field(default_factory=dict)
    params_override: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ValueError("PackExample.name must be str")
        if not isinstance(self.license, str):
            raise ValueError("PackExample.license must be str")
        if not isinstance(self.inputs, dict):
            raise ValueError("PackExample.inputs must be dict")
        if not isinstance(self.params_override, dict):
            raise ValueError("PackExample.params_override must be dict")


# ---------------------------------------------------------------------------
# Metrics / Assets / Skill
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsSpec:
    """``manifest.metrics`` — declared performance hints."""

    latency_ms: float = 0.0
    memory_mb: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("latency_ms", self.latency_ms),
            ("memory_mb", self.memory_mb),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"MetricsSpec.{name} must be number")
            if value < 0:
                raise ValueError(
                    f"MetricsSpec.{name} must be >= 0, got {value}"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetsSpec:
    """``manifest.assets`` — weights download metadata.

    The :attr:`checksum` is opaque (typically ``sha256:<hex>`` per the
    SSOT) — the Pack downloader / runner verifies it.
    """

    weights_url: str = ""
    checksum: str = ""
    size_bytes: int = 0
    install_path: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("weights_url", self.weights_url),
            ("checksum", self.checksum),
            ("install_path", self.install_path),
        ):
            if not isinstance(value, str):
                raise ValueError(f"AssetsSpec.{name} must be str")
        if not isinstance(self.size_bytes, int) or isinstance(
            self.size_bytes, bool
        ):
            raise ValueError("AssetsSpec.size_bytes must be int")
        if self.size_bytes < 0:
            raise ValueError(
                f"AssetsSpec.size_bytes must be >= 0, got {self.size_bytes}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillRef:
    """``manifest.skill`` — reference to an attached SKILL.md."""

    enabled: bool = False
    file: str = "SKILL.md"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("SkillRef.enabled must be bool")
        if not isinstance(self.file, str):
            raise ValueError("SkillRef.file must be str")


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class VariantSpec:
    """One row of ``manifest.variants``.

    SSOT ``multi-variant-pack-contract.md`` §1.1: a variant is a
    distinct implementation (FP16 / INT8 / ...) of the same model.
    Each variant has its own runtime / assets / metrics; the
    top-level manifest fields equal ``variants[default].*``.
    """

    id: str
    label: str = ""
    long_label: str = ""
    default: bool = False
    runtime: RuntimePack = field(default_factory=lambda: RuntimePack())
    assets: AssetsSpec = field(default_factory=lambda: AssetsSpec())
    metrics: MetricsSpec = field(default_factory=lambda: MetricsSpec())
    created_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("VariantSpec.id must be non-empty str")
        # Mirror multi-variant-pack-contract.md §1.4 alphabet.
        for ch in self.id:
            if not (ch.isalnum() or ch in "_-."):
                raise ValueError(
                    f"VariantSpec.id contains invalid char {ch!r}; "
                    "allowed: [a-zA-Z0-9_.-]"
                )
        if len(self.id) > 64:
            raise ValueError(
                f"VariantSpec.id must be ≤ 64 chars, got {len(self.id)}"
            )
        for name, value in (
            ("label", self.label),
            ("long_label", self.long_label),
            ("created_at", self.created_at),
        ):
            if not isinstance(value, str):
                raise ValueError(f"VariantSpec.{name} must be str")
        if not isinstance(self.default, bool):
            raise ValueError("VariantSpec.default must be bool")
        if not isinstance(self.runtime, RuntimePack):
            raise ValueError("VariantSpec.runtime must be RuntimePack")
        if not isinstance(self.assets, AssetsSpec):
            raise ValueError("VariantSpec.assets must be AssetsSpec")
        if not isinstance(self.metrics, MetricsSpec):
            raise ValueError("VariantSpec.metrics must be MetricsSpec")


# ---------------------------------------------------------------------------
# Top-level manifest aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PackManifest:
    """Decoded ``manifest.json`` root.

    Aggregates all the sub-VOs above into one immutable record. The
    manifest reader (in ``infrastructure/app_manifest/``) constructs
    this from a JSON dict and validates the schema version.
    """

    schema_version: int
    model_id: str
    display_name: str
    version: str = ""
    vendor: str = ""
    description: str = ""
    long_description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    runtime: RuntimePack = field(default_factory=lambda: RuntimePack())
    runner: PackRunnerSpec = field(default_factory=lambda: PackRunnerSpec())
    capabilities: Capabilities = field(default_factory=lambda: Capabilities())
    input_schema: PackInputSchema | None = None
    output_schema: PackOutputSchema | None = None
    params: tuple[PackParam, ...] = field(default_factory=tuple)
    metrics: MetricsSpec = field(default_factory=lambda: MetricsSpec())
    assets: AssetsSpec = field(default_factory=lambda: AssetsSpec())
    skill: SkillRef = field(default_factory=lambda: SkillRef())
    variants: tuple[VariantSpec, ...] = field(default_factory=tuple)
    # V1 parity: the manifest's ``taxonomy.tags`` (the curated display tags
    # shown in the CLASSIFICATION block / info drawer), distinct from the
    # top-level ``tags``. Tail-appended optional field.
    taxonomy_tags: tuple[str, ...] = field(default_factory=tuple)
    # V1 parity: the manifest's ``examples`` block (preset inputs /
    # params_override pairs the user can apply with one click from the info
    # drawer). Tail-appended optional field.
    examples: tuple[PackExample, ...] = field(default_factory=tuple)
    # Trilingual ``{en, zh-CN, zh-TW}`` default "Send to Chat" prompt a
    # generated WebUI app pre-fills by locale. Stored as a plain dict (mirrors
    # ``PackExample.inputs``; the manifest VO is never hashed). Tail-appended
    # optional field.
    send_to_chat_prompt: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise ValueError("PackManifest.schema_version must be int")
        if self.schema_version != 1:
            raise ValueError(
                f"Only schema_version=1 supported, got {self.schema_version}"
            )
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("PackManifest.model_id must be non-empty str")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError(
                "PackManifest.display_name must be non-empty str"
            )
        for name, value in (
            ("version", self.version),
            ("vendor", self.vendor),
            ("description", self.description),
            ("long_description", self.long_description),
        ):
            if not isinstance(value, str):
                raise ValueError(f"PackManifest.{name} must be str")
        if not isinstance(self.tags, tuple):
            raise ValueError("PackManifest.tags must be a tuple")
        for i, t in enumerate(self.tags):
            if not isinstance(t, str):
                raise ValueError(f"PackManifest.tags[{i}] must be str")
        if not isinstance(self.runtime, RuntimePack):
            raise ValueError("PackManifest.runtime must be RuntimePack")
        if not isinstance(self.runner, PackRunnerSpec):
            raise ValueError("PackManifest.runner must be PackRunnerSpec")
        if not isinstance(self.capabilities, Capabilities):
            raise ValueError("PackManifest.capabilities must be Capabilities")
        if self.input_schema is not None and not isinstance(
            self.input_schema, PackInputSchema
        ):
            raise ValueError(
                "PackManifest.input_schema must be PackInputSchema or None"
            )
        if self.output_schema is not None and not isinstance(
            self.output_schema, PackOutputSchema
        ):
            raise ValueError(
                "PackManifest.output_schema must be PackOutputSchema or None"
            )
        if not isinstance(self.params, tuple):
            raise ValueError("PackManifest.params must be a tuple")
        for i, p in enumerate(self.params):
            if not isinstance(p, PackParam):
                raise ValueError(f"PackManifest.params[{i}] must be PackParam")
        if not isinstance(self.metrics, MetricsSpec):
            raise ValueError("PackManifest.metrics must be MetricsSpec")
        if not isinstance(self.assets, AssetsSpec):
            raise ValueError("PackManifest.assets must be AssetsSpec")
        if not isinstance(self.skill, SkillRef):
            raise ValueError("PackManifest.skill must be SkillRef")
        if not isinstance(self.variants, tuple):
            raise ValueError("PackManifest.variants must be a tuple")
        for i, v in enumerate(self.variants):
            if not isinstance(v, VariantSpec):
                raise ValueError(f"PackManifest.variants[{i}] must be VariantSpec")
        if not isinstance(self.taxonomy_tags, tuple):
            raise ValueError("PackManifest.taxonomy_tags must be a tuple")
        for i, t in enumerate(self.taxonomy_tags):
            if not isinstance(t, str):
                raise ValueError(f"PackManifest.taxonomy_tags[{i}] must be str")
        if not isinstance(self.send_to_chat_prompt, dict):
            raise ValueError("PackManifest.send_to_chat_prompt must be a dict")
        for k, v in self.send_to_chat_prompt.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    "PackManifest.send_to_chat_prompt entries must be str->str"
                )
        # Multi-variant-pack-contract §1.4: at most one default.
        defaults = [v for v in self.variants if v.default]
        if len(defaults) > 1:
            raise ValueError(
                "manifest.variants may declare at most one default=true entry; "
                f"got {len(defaults)} ({[v.id for v in defaults]})"
            )
        # Variant IDs must be unique (key is id).
        seen: set[str] = set()
        for v in self.variants:
            if v.id in seen:
                raise ValueError(
                    f"manifest.variants[].id values must be unique; "
                    f"duplicate {v.id!r}"
                )
            seen.add(v.id)


# ---------------------------------------------------------------------------
# Variant selection (multi-variant-pack-contract §3)
# ---------------------------------------------------------------------------
def select_variant(
    manifest: PackManifest, variant_id: str | None
) -> VariantSpec | None:
    """Resolve ``variant_id`` against ``manifest.variants``.

    Resolution order (per SSOT ``multi-variant-pack-contract.md`` §3):

    1. If ``manifest.variants`` is empty → return ``None``
       (legacy single-variant pack — caller synthesises a default).
    2. If ``variant_id`` is a non-empty string → return the matching
       entry; raise :class:`ValueError` if no match (caller surfaces
       ``INVALID_VARIANT_ID``).
    3. If ``variant_id`` is ``None`` / empty → return the entry with
       ``default=true`` if any, else ``variants[0]`` (compat fallback).
    """
    if not manifest.variants:
        return None
    if variant_id is not None and variant_id.strip():
        target = variant_id.strip()
        for v in manifest.variants:
            if v.id == target:
                return v
        available = [v.id for v in manifest.variants]
        raise ValueError(
            f"variantId {target!r} not found in manifest.variants for "
            f"model {manifest.model_id!r}. Available: {available}"
        )
    # No variant_id requested — pick default, or fall back to first.
    for v in manifest.variants:
        if v.default:
            return v
    return manifest.variants[0]
