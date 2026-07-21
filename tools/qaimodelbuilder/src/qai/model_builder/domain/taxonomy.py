# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Two-level taxonomy SSOT for ``qai.model_builder``.

This module is a self-contained copy of the legacy
``backend/app_builder/taxonomy.py`` enum + lookup tables. It is kept
**inside** the model_builder context because:

* :class:`qai.app_builder.domain.taxonomy.Taxonomy` (present in this
  repo) is a *path value object* — a different concept;
* ``[importlinter:contract:context-isolation]`` forbids
  ``qai.model_builder`` from importing ``qai.app_builder``;
* duplicating ~120 lines of static tables is preferred over a shared
  kernel that would couple the two contexts.

The (group, task) tuples produced here also drive the legacy
``category`` reverse lookup used by the manifest builder so existing
v1.x ModelManifest consumers keep reading the same ``category``
strings.

Schema is documented under ``docs/30-ui-ux/model-taxonomy-redesign.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "TAXONOMY_VERSION",
    "GROUPS",
    "LEGACY_CATEGORY_MAP",
    "ClassifyResult",
    "all_task_ids",
    "all_group_ids",
    "task_label",
    "group_of_task",
    "io_for_task",
    "legacy_for",
    "as_dict",
]

# Bump whenever task/group enum changes; injected into LLM prompts so
# cached responses cannot drift across versions.
TAXONOMY_VERSION = "2026.05.22"


GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "audio",
        "label": "Audio",
        "icon": "audio",
        "tasks": (
            {
                "id": "audio-classification",
                "label": "Audio Classification",
                "description": "Predict tags / events / scenes from a clip.",
                "io": ("audio", "json"),
            },
            {
                "id": "audio-generation",
                "label": "Audio Generation",
                "description": "Synthesize speech / music / sound from text or conditions.",
                "io": ("text", "audio"),
            },
            {
                "id": "speech-recognition",
                "label": "Speech Recognition",
                "description": "Transcribe spoken audio to text (ASR).",
                "io": ("audio", "json"),
            },
            {
                "id": "audio-enhancement",
                "label": "Audio Enhancement",
                "description": "Denoise / dereverberate / source separation.",
                "io": ("audio", "audio"),
            },
            {
                "id": "speaker-verification",
                "label": "Speaker Verification",
                "description": "Speaker embedding & 1:1 / 1:N verification.",
                "io": ("audio", "json"),
            },
        ),
    },
    {
        "id": "computer-vision",
        "label": "Computer Vision",
        "icon": "vision",
        "tasks": (
            {
                "id": "image-classification",
                "label": "Image Classification",
                "description": "Predict a single class label for an image.",
                "io": ("image", "json"),
            },
            {
                "id": "object-detection",
                "label": "Object Detection",
                "description": "Locate and classify objects with bounding boxes.",
                "io": ("image", "json"),
            },
            {
                "id": "semantic-segmentation",
                "label": "Semantic Segmentation",
                "description": "Per-pixel class assignment for an image.",
                "io": ("image", "image"),
            },
            {
                "id": "pose-estimation",
                "label": "Pose Estimation",
                "description": "Detect keypoints of humans / objects in an image.",
                "io": ("image", "json"),
            },
            {
                "id": "depth-estimation",
                "label": "Depth Estimation",
                "description": "Predict per-pixel depth from a single image.",
                "io": ("image", "image"),
            },
            {
                "id": "super-resolution",
                "label": "Super Resolution",
                "description": "Upscale a low-resolution image to higher resolution.",
                "io": ("image", "image"),
            },
            {
                "id": "ocr",
                "label": "OCR",
                "description": "Detect and recognize text in an image (text detection + recognition).",
                "io": ("image", "json"),
            },
            {
                "id": "image-editing",
                "label": "Image Editing",
                "description": "Inpaint / outpaint / relight / restore images.",
                "io": ("image", "image"),
            },
            {
                "id": "video-classification",
                "label": "Video Classification",
                "description": "Predict a class label for a short video clip.",
                "io": ("multi", "json"),
            },
            {
                "id": "video-tracking",
                "label": "Video Object Tracking",
                "description": "Track one or many objects across video frames.",
                "io": ("multi", "json"),
            },
        ),
    },
    {
        "id": "generative-ai",
        "label": "Generative AI",
        "icon": "spark",
        "tasks": (
            {
                "id": "text-generation",
                "label": "Text Generation",
                "description": "Open-ended autoregressive text generation (LLMs).",
                "io": ("text", "text"),
            },
            {
                "id": "image-generation",
                "label": "Image Generation",
                "description": "Generate images from text / latent prompts (e.g. SD-Turbo).",
                "io": ("text", "image"),
            },
        ),
    },
    {
        "id": "multimodal",
        "label": "Multimodal",
        "icon": "stack",
        "tasks": (
            {
                "id": "image-to-text",
                "label": "Image to Text",
                "description": "Captioning / VQA — describe an image or answer a question about it.",
                "io": ("image", "text"),
            },
            {
                "id": "text-to-image",
                "label": "Text to Image",
                "description": "Cross-modal text-conditioned image synthesis.",
                "io": ("text", "image"),
            },
            {
                "id": "image-text-retrieval",
                "label": "Image-Text Retrieval",
                "description": "Joint image / text embedding for retrieval & matching (CLIP-like).",
                "io": ("multi", "json"),
            },
            {
                "id": "embedding",
                "label": "Embedding",
                "description": "Produce dense vectors for text / image (RAG / search / rerank).",
                "io": ("text", "json"),
            },
        ),
    },
)


# Order matters for legacy reverse lookup: the FIRST key whose value
# matches (group, task) wins. "LLM" precedes "NLP" so existing v1.x
# Pack manifests in the wild keep reading the same category string.
LEGACY_CATEGORY_MAP: dict[str, tuple[str, str | None]] = {
    "SR":         ("computer-vision", "super-resolution"),
    "OCR":        ("computer-vision", "ocr"),
    "ASR":        ("audio",           "speech-recognition"),
    "TTS":        ("audio",           "audio-generation"),
    "CV":         ("computer-vision", None),
    "LLM":        ("generative-ai",   "text-generation"),
    "NLP":        ("generative-ai",   "text-generation"),
    "Audio":      ("audio",           None),
    "Multimodal": ("multimodal",      None),
}


# ---------------------------------------------------------------------------
# Derived indexes (computed once at import; read-only at runtime)
#
# Each index is built once into a plain dict by ``_build_indexes`` and then
# frozen behind ``types.MappingProxyType`` so the module-level names expose a
# *read-only view*: ``[]`` / ``.get()`` / ``in`` / iteration all work, but the
# mappings cannot be reassigned, mutated or ``.clear()``-ed at runtime. This
# removes the "mutable global dict" surface entirely.
# ---------------------------------------------------------------------------


def _build_indexes() -> tuple[
    Mapping[str, dict[str, Any]],
    Mapping[str, str],
    Mapping[str, dict[str, Any]],
    Mapping[tuple[str, str], str],
]:
    task_index: dict[str, dict[str, Any]] = {}
    group_of_task: dict[str, str] = {}
    group_index: dict[str, dict[str, Any]] = {}
    legacy_reverse: dict[tuple[str, str], str] = {}
    for g in GROUPS:
        gid = g["id"]
        group_index[gid] = g
        for t in g["tasks"]:
            tid = t["id"]
            if tid in task_index:
                raise RuntimeError(
                    f"taxonomy: duplicate task id '{tid}' in groups "
                    f"'{group_of_task.get(tid)}' and '{gid}'"
                )
            task_index[tid] = t
            group_of_task[tid] = gid
    for legacy_key, (gid, tid) in LEGACY_CATEGORY_MAP.items():
        if tid is None:
            continue
        legacy_reverse.setdefault((gid, tid), legacy_key)
    return (
        MappingProxyType(task_index),
        MappingProxyType(group_of_task),
        MappingProxyType(group_index),
        MappingProxyType(legacy_reverse),
    )


_TASK_INDEX, _GROUP_OF_TASK, _GROUP_INDEX, _LEGACY_REVERSE = _build_indexes()


# ---------------------------------------------------------------------------
# Helpers (mirrors the legacy backend.app_builder.taxonomy public API)
# ---------------------------------------------------------------------------

def all_task_ids() -> list[str]:
    """Return every task id (flat list, declaration order)."""
    return list(_TASK_INDEX.keys())


def all_group_ids() -> list[str]:
    """Return every group id (declaration order)."""
    return [g["id"] for g in GROUPS]


def task_label(task_id: str) -> str | None:
    """Return the canonical English label of a task id (or ``None``)."""
    t = _TASK_INDEX.get(task_id)
    return t["label"] if t else None


def group_of_task(task_id: str) -> str | None:
    """Return the group id that owns this task (or ``None``)."""
    return _GROUP_OF_TASK.get(task_id)


def io_for_task(task_id: str) -> tuple[str, str] | None:
    """Return canonical ``(input_kind, output_kind)`` for a task."""
    t = _TASK_INDEX.get(task_id)
    if not t:
        return None
    io = t.get("io")
    if not io:
        return None
    return (str(io[0]), str(io[1]))


def legacy_for(group_id: str, task_id: str) -> str:
    """Reverse-lookup legacy ``category`` for a (group, task) pair.

    Returns the empty string when no legacy mapping exists; callers
    decide whether to fall back to a group-only string or omit the
    field entirely.
    """
    return _LEGACY_REVERSE.get((group_id, task_id), "")


def as_dict() -> dict[str, Any]:
    """Serializable representation suitable for the frontend / LLM prompt.

    Tuples in ``io`` are converted to lists so the result is JSON-safe.
    """
    out_groups: list[dict[str, Any]] = []
    for g in GROUPS:
        out_groups.append({
            "id":    g["id"],
            "label": g["label"],
            "icon":  g.get("icon"),
            "tasks": [
                {
                    "id":          t["id"],
                    "label":       t["label"],
                    "description": t.get("description", ""),
                    "io":          list(t.get("io", ())),
                }
                for t in g["tasks"]
            ],
        })
    return {
        "version": TAXONOMY_VERSION,
        "groups":  out_groups,
    }


# ---------------------------------------------------------------------------
# ClassifyResult value object (returned by adapters/taxonomy_classifier.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class ClassifyResult:
    """Outcome of a single classification call.

    ``task=None`` means the classifier could only narrow the model to
    a *group*; downstream code may fall back to a group-only legacy
    category string or surface the model as "uncategorised".
    """

    group: str
    task: str | None
    source: str  # one of: rule | shape | llm | manual | fallback
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "task": self.task,
            "source": self.source,
            "confidence": self.confidence,
        }
