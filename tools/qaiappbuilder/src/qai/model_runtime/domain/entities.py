# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain entities for the ``model_runtime`` bounded context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qai.model_runtime.domain.context_source_chain import resolve_context_length


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Represents a locally-available model on disk.

    Attributes:
        name: Human-readable model name / identifier (the model directory
            name, matching V1's ``model_dir.name``).
        path: File-system path to the model directory.
        size_mb: Approximate size in megabytes (0 if unknown).
        config_path: Absolute path to the model's ``config.json`` (empty
            string when the model has no config file). V1 surfaces this as
            ``config_path`` so the launch command can pin ``-c <config>``.
        model_format: Inference runtime format inferred from the on-disk
            files: ``"qnn"`` (NPU), ``"gguf"`` (GPU), ``"mnn"`` (CPU) or
            ``"unknown"``. Stored under the wire key ``format`` (see the
            route layer); the attribute is spelled ``model_format`` to
            avoid shadowing the builtin while keeping the entity framework
            free.
        context_length: Context-window size discovered from model metadata
            (``dialog.context.size`` -> prompt ``context_size`` -> top-level
            config fields). ``None`` means no authoritative value was
            found — callers must not fabricate a context window. The
            ``/api/service/models`` payload appends it under the wire key
            ``context_length`` so the chat model dropdown can show a
            "8K"/"32K" badge for local models (hidden when ``None``).
        supports_audio: Whether this local model family accepts audio
            input. Currently ``True`` only for the ``qwen2.5_omni``
            family (matched by the ``"omni"`` substring in the model
            directory name).
    """

    name: str
    path: str
    size_mb: float
    config_path: str = ""
    model_format: str = "unknown"
    context_length: int | None = None
    supports_audio: bool = False


def detect_model_format(
    file_names: list[str], file_suffixes: list[str]
) -> str:
    """Infer a model's inference runtime format from its on-disk files.

    Pure helper (no I/O) so it can live in the domain layer and be unit
    tested in isolation. Mirrors V1's ``_detect_model_format``:

    - ``.gguf`` present                                  -> ``"gguf"`` (GPU)
    - ``.mnn`` present                                   -> ``"mnn"`` (CPU)
    - ``.bin`` present *and* ``tokenizer.json`` present  -> ``"qnn"`` (NPU)
    - otherwise                                          -> ``"unknown"``

    Args:
        file_names: Lower-cased file names directly inside the model dir.
        file_suffixes: Lower-cased suffixes (``Path.suffix``) of those
            files (e.g. ``".gguf"``).

    Returns:
        One of ``"gguf"``, ``"mnn"``, ``"qnn"`` or ``"unknown"``.
    """
    suffixes = set(file_suffixes)
    names = set(file_names)
    if ".gguf" in suffixes:
        return "gguf"
    if ".mnn" in suffixes:
        return "mnn"
    if ".bin" in suffixes and "tokenizer.json" in names:
        return "qnn"
    return "unknown"


def has_unsafe_path(path: str) -> bool:
    """Return True if *path* contains non-ASCII characters or spaces.

    Mirrors V1's ``hasUnsafePath`` / ``_has_unsafe_chars``: GenieAPIService's
    QNN backend converts paths Unicode->ANSI at init time, so paths with
    Chinese characters or spaces can break model loading. Pure helper (no
    I/O) suitable for the domain layer.
    """
    if not path:
        return False
    return any(ord(c) > 127 or c == " " for c in path)


def extract_context_length(
    config: dict | None,
    prompt: dict | None = None,
) -> int | None:
    """Return context length discovered from local model metadata.

    Pure helper (no I/O) living in the domain layer; delegates to
    :func:`qai.model_runtime.domain.context_source_chain.resolve_context_length`.

    Resolution checks ``dialog.context.size``, then prompt metadata, then
    top-level config fields. Missing or invalid metadata remains ``None``;
    callers must not fabricate a model context window.

    Args:
        config: Parsed ``config.json`` mapping (``None`` when absent /
            unreadable).
        prompt: Parsed ``prompt.json`` mapping (``None`` when the model
            has no ``prompt.json``).

    Returns:
        A positive context-window size, or ``None`` when nothing resolves.
    """
    config_map: Mapping[str, Any] | None = (
        config if isinstance(config, Mapping) else None
    )
    prompt_map: Mapping[str, Any] | None = (
        prompt if isinstance(prompt, Mapping) else None
    )
    return resolve_context_length(config_map, prompt_map)


__all__ = [
    "ModelInfo",
    "detect_model_format",
    "extract_context_length",
    "has_unsafe_path",
]
