# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem ``manifest.json`` reader.

Given a Pack root directory, finds and decodes all
``<root>/<model_id>/manifest.json`` files into typed
:class:`qai.app_builder.domain.pack_manifest.PackManifest` instances.

Field-name translation
----------------------

Manifest JSON uses camelCase (web tradition) while the domain VOs use
snake_case. The reader does the translation:

================================  ==============================
JSON wire (camelCase)             Domain field (snake_case)
================================  ==============================
``modelId``                       ``model_id``
``displayName``                   ``display_name``
``longDescription``               ``long_description``
``modelSizeMB``                   ``model_size_mb``
``contextBins``                   ``context_bins``
``supportedDevices``              ``supported_devices``
``timeoutMs``                     ``timeout_ms``
``inputSchema``                   ``input_schema``
``outputSchema``                  ``output_schema``
``jsonSchema``                    ``json_schema``
``weightsUrl``                    ``weights_url``
``sizeBytes``                     ``size_bytes``
``installPath``                   ``install_path``
``latencyMs``                     ``latency_ms``
``memoryMB``                      ``memory_mb``
``createdAt``                     ``created_at``
================================  ==============================

Robustness
----------

* Missing optional fields fall back to sensible defaults (defined on
  the domain VOs).
* Type-mismatched fields trigger :class:`ManifestParseError` with the
  offending key path so operators can fix the manifest.
* The reader never imports legacy ``backend.*`` / ``features.*`` —
  it only reads bytes from disk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from qai.app_builder.domain.pack_manifest import (
    AssetsSpec,
    Capabilities,
    InputKind,
    MetricsSpec,
    OutputKind,
    PackInputSchema,
    PackManifest,
    PackOutputSchema,
    PackParam,
    PackRunnerSpec,
    RuntimePack,
    SkillRef,
    VariantSpec,
)

__all__ = ["FileSystemManifestReader", "ManifestParseError"]

logger = logging.getLogger(__name__)


class ManifestParseError(ValueError):
    """Raised when a manifest.json cannot be decoded into a PackManifest.

    Carries the offending file path + the JSON key path (dot-joined).
    """

    def __init__(
        self,
        *,
        file: Path,
        message: str,
        key_path: str = "",
    ) -> None:
        scope = f"{file}:{key_path}" if key_path else str(file)
        super().__init__(f"{scope}: {message}")
        self.file = file
        self.key_path = key_path


class FileSystemManifestReader:
    """Read ``<root>/<model_id>/manifest.json`` files into PackManifests.

    The reader does NOT crawl arbitrary nesting — it expects exactly
    one level of subdirectories below ``root`` (one per Pack), each
    containing a ``manifest.json``. Subdirectories without a manifest
    file are skipped silently.
    """

    __slots__ = ("_strict",)

    def __init__(self, *, strict: bool = False) -> None:
        """``strict``: when ``True``, a single bad manifest aborts
        :meth:`read_all`; when ``False`` (default), bad manifests are
        logged and skipped so good packs still load.
        """
        self._strict = bool(strict)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def read_all(self, root: Path) -> tuple[PackManifest, ...]:
        """Read every ``manifest.json`` directly below ``root``.

        Returns the tuple in deterministic (sorted-by-dir-name) order.
        """
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if not root.is_dir():
            return ()
        manifests: list[PackManifest] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifests.append(self.read_one(manifest_path))
            except ManifestParseError as exc:
                if self._strict:
                    raise
                logger.warning(
                    "skipping unreadable manifest at %s: %s",
                    manifest_path, exc,
                )
        return tuple(manifests)

    def read_one(self, file: Path) -> PackManifest:
        """Read and decode a single ``manifest.json`` file.

        Raises :class:`ManifestParseError` on malformed JSON / unknown
        schema_version / type-mismatched required fields.
        """
        if not isinstance(file, Path):
            raise TypeError("file must be a Path")
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestParseError(
                file=file, message=f"cannot read: {exc}"
            ) from exc
        try:
            obj: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestParseError(
                file=file, message=f"invalid JSON: {exc}",
            ) from exc
        if not isinstance(obj, dict):
            raise ManifestParseError(
                file=file, message="root must be a JSON object"
            )
        try:
            return _decode_manifest(obj, manifest_file=file)
        except (ValueError, TypeError) as exc:
            raise ManifestParseError(
                file=file, message=str(exc)
            ) from exc


# ---------------------------------------------------------------------------
# Decoders (pure functions — easy to unit test)
# ---------------------------------------------------------------------------
def _decode_manifest(
    obj: dict[str, Any], *, manifest_file: Path | None = None
) -> PackManifest:
    """Decode a manifest dict into a :class:`PackManifest`.

    ``manifest_file``: when provided (always provided by
    :meth:`FileSystemManifestReader.read_one`), used as the anchor for
    resolving the ``runner.requirements`` relative path to an absolute
    one. Without an anchor, downstream callers
    (:func:`qai.app_builder.application.use_cases.run_app._extract_pack_deps`)
    silently no-op because ``Path("requirements.txt").is_file()`` is
    evaluated against the API server's working directory rather than the
    pack root. B4-16 fix: do the resolution at the reader layer so every
    consumer sees the absolute path verbatim. ``None`` (the default)
    preserves the pre-fix behaviour for the rare unit-test caller that
    passes a hand-built dict; production reads always go through
    :meth:`read_one` and therefore always carry the anchor.
    """
    schema_version = obj.get("schema_version")
    return PackManifest(
        schema_version=int(schema_version) if isinstance(schema_version, int) else 1,
        model_id=_str(obj, "modelId", required=True),
        display_name=_str(obj, "displayName", required=True),
        version=_str(obj, "version"),
        vendor=_str(obj, "vendor"),
        description=_str(obj, "description"),
        long_description=_str(obj, "longDescription"),
        tags=_str_tuple(obj, "tags"),
        runtime=_decode_runtime(obj.get("runtime")),
        runner=_decode_runner(
            obj.get("runner"), manifest_file=manifest_file
        ),
        capabilities=_decode_capabilities(obj.get("capabilities")),
        input_schema=_decode_input_schema(obj.get("inputSchema")),
        output_schema=_decode_output_schema(obj.get("outputSchema")),
        params=_decode_params(obj.get("params")),
        metrics=_decode_metrics(obj.get("metrics")),
        assets=_decode_assets(obj.get("assets")),
        skill=_decode_skill(obj.get("skill")),
        variants=_decode_variants(obj.get("variants")),
        taxonomy_tags=_decode_taxonomy_tags(obj.get("taxonomy")),
        examples=_decode_examples(obj.get("examples")),
        send_to_chat_prompt=_decode_send_to_chat_prompt(
            obj.get("sendToChatPrompt")
        ),
    )


def _decode_send_to_chat_prompt(obj: Any) -> dict[str, str]:
    """Extract the manifest's trilingual ``sendToChatPrompt`` block.

    Shape: ``{en, zh-CN, zh-TW}`` — the locale-keyed default prompt a generated
    WebUI app pre-fills into its "Send to Chat" box. Best-effort: a missing /
    non-dict value yields ``{}`` and non-string entries are dropped, so a
    malformed prompt never blocks the whole manifest (mirrors
    :func:`_decode_taxonomy_tags`).
    """
    if not isinstance(obj, dict):
        return {}
    return {
        k: v
        for k, v in obj.items()
        if isinstance(k, str) and isinstance(v, str) and v
    }


def _decode_taxonomy_tags(obj: Any) -> tuple[str, ...]:
    """Extract the manifest's ``taxonomy.tags`` (V1 display tags).

    The manifest ``taxonomy`` block is an object ``{group, task, tags}``; we
    pull just the ``tags`` list (the curated display tags shown in the
    CLASSIFICATION block / info drawer). Returns an empty tuple when absent or
    malformed (best-effort; V1 simply renders no tags in that case).
    """
    if not isinstance(obj, dict):
        return ()
    tags = obj.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(str(t) for t in tags if isinstance(t, str) and t)


def _decode_examples(obj: Any) -> tuple["PackExample", ...]:
    """Decode ``manifest.examples`` — list of preset input bundles.

    Each element is ``{name?, license?, inputs?, params_override?}``.
    Gracefully skips malformed entries.
    """
    from qai.app_builder.domain.pack_manifest import PackExample

    if not isinstance(obj, list):
        return ()
    out: list[PackExample] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        out.append(
            PackExample(
                name=item.get("name", "") if isinstance(item.get("name"), str) else "",
                license=item.get("license", "") if isinstance(item.get("license"), str) else "",
                inputs=item.get("inputs", {}) if isinstance(item.get("inputs"), dict) else {},
                params_override=(
                    item.get("params_override", {})
                    if isinstance(item.get("params_override"), dict)
                    else item.get("paramsOverride", {})
                    if isinstance(item.get("paramsOverride"), dict)
                    else {}
                ),
            )
        )
    return tuple(out)


def _decode_runtime(obj: Any) -> RuntimePack:
    if obj is None:
        return RuntimePack()
    if not isinstance(obj, dict):
        raise ValueError("runtime must be a JSON object")
    return RuntimePack(
        backend=_str(obj, "backend"),
        delegate=_str(obj, "delegate"),
        quantization=_str(obj, "quantization"),
        model_size_mb=_int(obj, "modelSizeMB"),
        context_bins=_str_tuple(obj, "contextBins"),
        supported_devices=_str_tuple(obj, "supportedDevices"),
    )


def _decode_runner(
    obj: Any, *, manifest_file: Path | None = None
) -> PackRunnerSpec:
    """Decode the ``manifest.runner`` block.

    B4-16: when ``runner.requirements`` is a non-empty *relative* path,
    resolve it against ``manifest_file.parent`` so downstream consumers
    (notably
    :func:`qai.app_builder.application.use_cases.run_app._extract_pack_deps`)
    receive an absolute path that ``Path.is_file()`` can probe regardless
    of the API server's current working directory. Absolute paths and
    empty values are passed through unchanged — matching legacy
    ``backend/app_builder/...`` semantics.
    """
    if obj is None:
        return PackRunnerSpec()
    if not isinstance(obj, dict):
        raise ValueError("runner must be a JSON object")
    raw_requirements = _str(obj, "requirements")
    requirements = _resolve_pack_relative_path(
        raw_requirements, manifest_file=manifest_file
    )
    return PackRunnerSpec(
        type=_str(obj, "type", default="python-script"),
        script=_str(obj, "script", default="runner.py"),
        venv=_str(obj, "venv", default="arm64"),
        requirements=requirements,
        timeout_ms=_int(obj, "timeoutMs"),
    )


def _resolve_pack_relative_path(
    raw: str, *, manifest_file: Path | None
) -> str:
    """Resolve ``raw`` against the manifest's parent directory if relative.

    Returns ``raw`` unchanged when:

    * ``raw`` is empty (the schema's "no requirements file" sentinel);
    * ``manifest_file`` is ``None`` (no anchor available — pre-B4-16
      behaviour preserved for hand-built unit-test dicts);
    * ``raw`` is already absolute.

    Otherwise returns ``str(manifest_file.parent / raw)`` so the consumer
    has a path that ``Path.is_file()`` can probe directly.
    """
    if not raw:
        return raw
    if manifest_file is None:
        return raw
    candidate = Path(raw)
    if candidate.is_absolute():
        return raw
    return str((manifest_file.parent / candidate).resolve())


def _decode_capabilities(obj: Any) -> Capabilities:
    if obj is None:
        return Capabilities()
    if not isinstance(obj, dict):
        raise ValueError("capabilities must be a JSON object")
    return Capabilities(
        streaming=_bool(obj, "streaming", default=False),
        batch=_bool(obj, "batch", default=False),
        benchmark=_bool(obj, "benchmark", default=False),
        cancel=_bool(obj, "cancel", default=True),
    )


def _decode_input_schema(obj: Any) -> PackInputSchema | None:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ValueError("inputSchema must be a JSON object")
    kind = _str(obj, "kind", required=True)
    if kind not in ("audio", "image", "text", "json", "video"):
        raise ValueError(
            f"inputSchema.kind must be one of audio|image|text|json|video, "
            f"got {kind!r}"
        )
    constraints_obj = obj.get("constraints", {})
    if not isinstance(constraints_obj, dict):
        raise ValueError("inputSchema.constraints must be a JSON object")
    return PackInputSchema(
        kind=kind,  # type: ignore[arg-type]
        constraints=tuple(constraints_obj.items()),
    )


def _decode_output_schema(obj: Any) -> PackOutputSchema | None:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ValueError("outputSchema must be a JSON object")
    kind = _str(obj, "kind", required=True)
    if kind not in ("audio", "image", "text", "json", "video"):
        raise ValueError(
            f"outputSchema.kind must be one of audio|image|text|json|video, "
            f"got {kind!r}"
        )
    constraints_obj = obj.get("constraints", {})
    if not isinstance(constraints_obj, dict):
        raise ValueError("outputSchema.constraints must be a JSON object")
    json_schema_obj = obj.get("jsonSchema")
    json_schema_tuple: tuple[tuple[str, Any], ...] | None
    if json_schema_obj is None:
        json_schema_tuple = None
    elif isinstance(json_schema_obj, dict):
        json_schema_tuple = tuple(json_schema_obj.items())
    else:
        raise ValueError("outputSchema.jsonSchema must be JSON object or null")
    return PackOutputSchema(
        kind=kind,  # type: ignore[arg-type]
        constraints=tuple(constraints_obj.items()),
        json_schema=json_schema_tuple,
    )


def _decode_params(obj: Any) -> tuple[PackParam, ...]:
    if obj is None:
        return ()
    if not isinstance(obj, list):
        raise ValueError("params must be a JSON array")
    out: list[PackParam] = []
    for i, p in enumerate(obj):
        if not isinstance(p, dict):
            raise ValueError(f"params[{i}] must be a JSON object")

        def _num(d: dict[str, Any], key: str) -> float | None:
            v = d.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        raw_opts = p.get("options")
        options = (
            tuple(raw_opts) if isinstance(raw_opts, list) and len(raw_opts) > 0 else None
        )
        out.append(
            PackParam(
                name=_str(p, "name", required=True),
                label=_str(p, "label"),
                type=_str(p, "type", default="string"),
                default=p.get("default"),
                min=_num(p, "min"),
                max=_num(p, "max"),
                step=_num(p, "step"),
                options=options,
                advanced=p.get("advanced") is True,
            )
        )
    return tuple(out)


def _decode_metrics(obj: Any) -> MetricsSpec:
    if obj is None:
        return MetricsSpec()
    if not isinstance(obj, dict):
        raise ValueError("metrics must be a JSON object")
    return MetricsSpec(
        latency_ms=_float(obj, "latencyMs"),
        memory_mb=_float(obj, "memoryMB"),
    )


def _decode_assets(obj: Any) -> AssetsSpec:
    if obj is None:
        return AssetsSpec()
    if not isinstance(obj, dict):
        raise ValueError("assets must be a JSON object")
    return AssetsSpec(
        weights_url=_str(obj, "weightsUrl"),
        checksum=_str(obj, "checksum"),
        size_bytes=_int(obj, "sizeBytes"),
        install_path=_str(obj, "installPath"),
    )


def _decode_skill(obj: Any) -> SkillRef:
    if obj is None:
        return SkillRef()
    if not isinstance(obj, dict):
        raise ValueError("skill must be a JSON object")
    return SkillRef(
        enabled=_bool(obj, "enabled", default=False),
        file=_str(obj, "file", default="SKILL.md"),
    )


def _decode_variants(obj: Any) -> tuple[VariantSpec, ...]:
    if obj is None:
        return ()
    if not isinstance(obj, list):
        raise ValueError("variants must be a JSON array")
    out: list[VariantSpec] = []
    for i, v in enumerate(obj):
        if not isinstance(v, dict):
            raise ValueError(f"variants[{i}] must be a JSON object")
        out.append(
            VariantSpec(
                id=_str(v, "id", required=True),
                label=_str(v, "label"),
                long_label=_str(v, "longLabel"),
                default=_bool(v, "default", default=False),
                runtime=_decode_runtime(v.get("runtime")),
                assets=_decode_assets(v.get("assets")),
                metrics=_decode_metrics(v.get("metrics")),
                created_at=_str(v, "createdAt"),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Field extractors — strict typing with default fallback
# ---------------------------------------------------------------------------
_MISSING = object()


def _str(
    obj: dict[str, Any],
    key: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise ValueError(f"required field {key!r} missing")
        return default
    if not isinstance(value, str):
        raise ValueError(f"field {key!r} must be str, got {type(value).__name__}")
    return value


def _str_tuple(obj: dict[str, Any], key: str) -> tuple[str, ...]:
    value = obj.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"field {key!r} must be a JSON array of str")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"field {key}[{i}] must be str")
        out.append(item)
    return tuple(out)


def _int(obj: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"field {key!r} must be int, got bool")
    if not isinstance(value, int):
        # Tolerant conversion from float (some manifests emit 0.0).
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"field {key!r} must be int, got {type(value).__name__}")
    return value


def _float(obj: dict[str, Any], key: str, *, default: float = 0.0) -> float:
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"field {key!r} must be number, got bool")
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"field {key!r} must be number, got {type(value).__name__}"
        )
    return float(value)


def _bool(
    obj: dict[str, Any], key: str, *, default: bool = False
) -> bool:
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(
            f"field {key!r} must be bool, got {type(value).__name__}"
        )
    return value


# Suppress unused-import warning.
_ = (Iterable, InputKind, OutputKind)
