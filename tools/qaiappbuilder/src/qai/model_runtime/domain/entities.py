# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain entities for the ``model_runtime`` bounded context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qai.model_runtime.domain.context_source_chain import (
    DEFAULT_CONTEXT_SIZE,
    resolve_context_length,
)


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
        context_length: Context-window size (tokens) resolved from the
            model's metadata via the V1 4-source cascade (config.json
            ``dialog.context.size`` -> prompt.json ``context_size`` ->
            config.json top-level ``context_size`` / ``context_length`` /
            ``max_position_embeddings`` -> default ``8192``). V1 surfaces
            this as the dropdown ctx badge (``index.html:1025`` +
            ``useModels.js:156-161``); the V2 ``/api/service/models``
            payload appends it under the wire key ``context_length`` so
            the chat model dropdown can show the same "8K"/"32K" badge
            for local models. Never ``0`` for a successfully scanned
            model (V1 parity — defaults to 8192).
    """

    name: str
    path: str
    size_mb: float
    config_path: str = ""
    model_format: str = "unknown"
    context_length: int = 0


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
) -> int:
    """Return the context-window size from parsed model metadata.

    Pure helper (no I/O) living in the domain layer; delegates to
    :func:`qai.model_runtime.domain.context_source_chain.resolve_context_length`
    which mirrors V1's ``backend/models_registry.py:_read_context_size_from_model_dir``
    (lines 233-279) 4-source priority cascade:

    1. ``config.json`` ``dialog.context.size`` (QNN/SSD authoritative)
    2. ``prompt.json`` ``context_size`` (GenieAPIService ParsePromptFile)
    3. ``config.json`` top-level ``context_size`` / ``context_length`` /
       ``max_position_embeddings`` (GGUF/MNN)
    4. default ``8192`` (:data:`DEFAULT_CONTEXT_SIZE`)

    Unlike the pre-U-007b implementation (which only read source 1 and
    returned ``0`` on a miss), this now restores V1's full cascade and
    the ``8192`` fallback, so GGUF/MNN models whose ctx lives at the
    config top level — or models with no readable size at all — surface
    a non-zero badge exactly as V1 did.

    Args:
        config: Parsed ``config.json`` mapping (``None`` when absent /
            unreadable).
        prompt: Parsed ``prompt.json`` mapping (``None`` when the model
            has no ``prompt.json``).

    Returns:
        A positive context-window size; ``8192`` when nothing resolves.
    """
    config_map: Mapping[str, Any] | None = (
        config if isinstance(config, Mapping) else None
    )
    prompt_map: Mapping[str, Any] | None = (
        prompt if isinstance(prompt, Mapping) else None
    )
    return resolve_context_length(config_map, prompt_map)


__all__ = [
    "DEFAULT_CONTEXT_SIZE",
    "ModelInfo",
    "detect_model_format",
    "extract_context_length",
    "has_unsafe_path",
]
