# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder taxonomy tree (single source of truth).

Defines the two-level ``group → task`` taxonomy used by the App Builder
workbench setup-bar picker. This mirrors the legacy backend's
``backend/app_builder/taxonomy.py`` ``GROUPS`` table (the verified V1 source
of truth) so the V2 ``GET /taxonomy/tree`` endpoint can serve the full set of
human-readable group / task labels, icons, descriptions and IO kinds — not
just the distinct paths that happen to have a registered model.

Why this lives in the domain layer (architecture rationale):

* The taxonomy is a *business vocabulary* (the catalogue of model
  capabilities the product supports), not an adapter / framework concern, so
  it belongs in ``domain``. It is pure data + pure helpers (no FastAPI /
  SQLAlchemy / settings imports), keeping ``domain`` framework-free.
* V1 duplicated this table in BOTH the backend (``taxonomy.py``) and the
  frontend (``useAppBuilderRegistry.FALLBACK_TAXONOMY``). V2 keeps a *single*
  authoritative copy here and the frontend consumes it over the wire, so the
  human-readable labels never drift between front and back.

Bump :data:`TAXONOMY_VERSION` whenever a task / group is added, renamed or
removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "TAXONOMY_VERSION",
    "TaxonomyTask",
    "TaxonomyGroup",
    "GROUPS",
    "group_label",
    "group_icon",
    "task_label",
    "iter_tasks",
]

# Bump when the task / group set changes (front + back depend on it).
TAXONOMY_VERSION = "2026.05.22"


@dataclass(frozen=True, slots=True)
class TaxonomyTask:
    """A leaf task in the taxonomy tree.

    ``io`` is the ``(input_kind, output_kind)`` pair, aligned with the
    frontend ``ModelManifest.inputSchema.kind`` vocabulary
    (image / audio / text / multi / json).
    """

    id: str
    label: str
    description: str
    io: tuple[str, str]


@dataclass(frozen=True, slots=True)
class TaxonomyGroup:
    """A top-level group owning a set of :class:`TaxonomyTask`."""

    id: str
    label: str
    icon: str
    tasks: tuple[TaxonomyTask, ...] = field(default_factory=tuple)


# Verbatim port of V1 ``backend/app_builder/taxonomy.py`` GROUPS (the verified
# source of truth). Implementation differs (typed dataclasses vs raw dicts) so
# the domain stays type-safe, but the *vocabulary* is identical for parity.
GROUPS: tuple[TaxonomyGroup, ...] = (
    TaxonomyGroup(
        id="audio",
        label="Audio",
        icon="audio",
        tasks=(
            TaxonomyTask(
                id="audio-classification",
                label="Audio Classification",
                description="Predict tags / events / scenes from a clip.",
                io=("audio", "json"),
            ),
            TaxonomyTask(
                id="audio-generation",
                label="Audio Generation",
                description="Synthesize speech / music / sound from text or conditions.",
                io=("text", "audio"),
            ),
            TaxonomyTask(
                id="speech-recognition",
                label="Speech Recognition",
                description="Transcribe spoken audio to text (ASR).",
                io=("audio", "json"),
            ),
            TaxonomyTask(
                id="audio-enhancement",
                label="Audio Enhancement",
                description="Denoise / dereverberate / source separation.",
                io=("audio", "audio"),
            ),
            TaxonomyTask(
                id="speaker-verification",
                label="Speaker Verification",
                description="Speaker embedding & 1:1 / 1:N verification.",
                io=("audio", "json"),
            ),
        ),
    ),
    TaxonomyGroup(
        id="computer-vision",
        label="Computer Vision",
        icon="vision",
        tasks=(
            TaxonomyTask(
                id="image-classification",
                label="Image Classification",
                description="Predict a single class label for an image.",
                io=("image", "json"),
            ),
            TaxonomyTask(
                id="object-detection",
                label="Object Detection",
                description="Locate and classify objects with bounding boxes.",
                io=("image", "json"),
            ),
            TaxonomyTask(
                id="semantic-segmentation",
                label="Semantic Segmentation",
                description="Per-pixel class assignment for an image.",
                io=("image", "image"),
            ),
            TaxonomyTask(
                id="pose-estimation",
                label="Pose Estimation",
                description="Detect keypoints of humans / objects in an image.",
                io=("image", "json"),
            ),
            TaxonomyTask(
                id="depth-estimation",
                label="Depth Estimation",
                description="Predict per-pixel depth from a single image.",
                io=("image", "image"),
            ),
            TaxonomyTask(
                id="super-resolution",
                label="Super Resolution",
                description="Upscale a low-resolution image to higher resolution.",
                io=("image", "image"),
            ),
            TaxonomyTask(
                id="ocr",
                label="OCR",
                description="Detect and recognize text in an image (text detection + recognition).",
                io=("image", "json"),
            ),
            TaxonomyTask(
                id="image-editing",
                label="Image Editing",
                description="Inpaint / outpaint / relight / restore images.",
                io=("image", "image"),
            ),
            TaxonomyTask(
                id="video-classification",
                label="Video Classification",
                description="Predict a class label for a short video clip.",
                io=("multi", "json"),
            ),
            TaxonomyTask(
                id="video-tracking",
                label="Video Object Tracking",
                description="Track one or many objects across video frames.",
                io=("multi", "json"),
            ),
        ),
    ),
    TaxonomyGroup(
        id="generative-ai",
        label="Generative AI",
        icon="spark",
        tasks=(
            TaxonomyTask(
                id="text-generation",
                label="Text Generation",
                description="Open-ended autoregressive text generation (LLMs).",
                io=("text", "text"),
            ),
            TaxonomyTask(
                id="image-generation",
                label="Image Generation",
                description="Generate images from text / latent prompts (e.g. SD-Turbo).",
                io=("text", "image"),
            ),
        ),
    ),
    TaxonomyGroup(
        id="multimodal",
        label="Multimodal",
        icon="stack",
        tasks=(
            TaxonomyTask(
                id="image-to-text",
                label="Image to Text",
                description="Captioning / VQA — describe an image or answer a question about it.",
                io=("image", "text"),
            ),
            TaxonomyTask(
                id="text-to-image",
                label="Text to Image",
                description="Cross-modal text-conditioned image synthesis.",
                io=("text", "image"),
            ),
            TaxonomyTask(
                id="image-text-retrieval",
                label="Image-Text Retrieval",
                description="Joint image / text embedding for retrieval & matching (CLIP-like).",
                io=("multi", "json"),
            ),
            TaxonomyTask(
                id="embedding",
                label="Embedding",
                description="Produce dense vectors for text / image (RAG / search / rerank).",
                io=("text", "json"),
            ),
        ),
    ),
)


# ── Derived indexes (built once at import; read-only at runtime) ───────────
_GROUP_INDEX: dict[str, TaxonomyGroup] = {g.id: g for g in GROUPS}
_TASK_INDEX: dict[str, TaxonomyTask] = {
    t.id: t for g in GROUPS for t in g.tasks
}


def group_label(group_id: str) -> str | None:
    """Human-readable label for a group id, or ``None`` if unknown."""
    g = _GROUP_INDEX.get(group_id)
    return g.label if g is not None else None


def group_icon(group_id: str) -> str | None:
    """Icon token for a group id, or ``None`` if unknown."""
    g = _GROUP_INDEX.get(group_id)
    return g.icon if g is not None else None


def task_label(task_id: str) -> str | None:
    """Human-readable label for a task id, or ``None`` if unknown."""
    t = _TASK_INDEX.get(task_id)
    return t.label if t is not None else None


def iter_tasks() -> tuple[tuple[str, TaxonomyTask], ...]:
    """All ``(group_id, task)`` pairs in declaration order."""
    return tuple((g.id, t) for g in GROUPS for t in g.tasks)
