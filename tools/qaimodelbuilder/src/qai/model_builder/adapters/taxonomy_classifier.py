# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Three-layer taxonomy classifier (rule → shape → optional LLM).

Adapter implementation of
:class:`qai.model_builder.application.ports.TaxonomyClassifierPort`.

Pipeline (mirrors the legacy
``backend/app_builder/taxonomy_classifier.classify``):

1. **Rule layer** — keyword match against the bundled rules table
   (see :data:`_TASK_KEYWORDS` / :data:`_GROUP_KEYWORDS`). Confidence
   ``0.95`` for task hit, ``0.6`` for group-only hit.
2. **Shape layer** — input/output dim cues from the inference
   manifest. Confidence ``0.85`` / ``0.7`` / ``0.5``.
3. **LLM layer** — only invoked when the optional ``llm_callable``
   is wired by DI. Confidence whatever the LLM emits, validated
   against the taxonomy enum.

The legacy script loaded the keyword tables from a YAML file. We
inline them here to avoid a YAML runtime dependency on the API
process — the table is small enough to maintain in code, and
freezing it as a Python tuple makes the rule layer hot-load-free.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qai.model_builder.domain import (
    ClassifyResult,
    all_task_ids,
    group_of_task,
    io_for_task,
)

__all__ = [
    "RuleAndShapeTaxonomyClassifier",
    "LLMClassifyCallable",
]


# Type alias for the optional LLM callable. The contract mirrors the
# legacy ``llm_callable`` used by ``taxonomy_classifier.classify``:
# given the model name + a constrained-enum prompt, return either a
# single task id, a single group id, or the empty string.
LLMClassifyCallable = Callable[[str, str], str]


# ---------------------------------------------------------------------------
# Rule tables (verbatim copy of features/model-builder/scripts/taxonomy_rules.yaml)
# ---------------------------------------------------------------------------

_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "super-resolution": (
        "esrgan", "srgan", "swinir", "edsr", "rcan", "hat-sr",
        "real-esrgan", "realesrgan", "realsr",
    ),
    "ocr": (
        "ocr", "ppocr", "paddleocr", "dbnet", "crnn", "east-text",
    ),
    "speech-recognition": (
        "whisper", "zipformer", "conformer-asr", "wenet",
        "deepspeech", "paraformer", "wav2vec",
    ),
    "audio-generation": (
        "tts", "melotts", "vits", "fastspeech", "hifigan",
        "wavenet", "tacotron", "bark-tts",
    ),
    "text-generation": (
        "llama", "qwen", "phi-2", "phi-3", "mistral", "gemma",
        "chatglm", "baichuan", "yi-6b", "yi-9b", "yi-34b",
    ),
    "object-detection": (
        "yolo", "yolov", "yolox", "ssd", "fcos", "detr",
        "rtmdet", "nanodet", "retinanet",
    ),
    "image-classification": (
        "resnet", "mobilenet", "efficientnet-lite", "efficientnet-v2",
        "regnet", "convnext", "inception", "densenet",
        "shufflenet", "squeezenet",
    ),
    "semantic-segmentation": (
        "deeplabv3", "segformer", "u2net", "unet", "maskrcnn",
    ),
    "pose-estimation": (
        "movenet", "rtmpose", "hrnet-pose", "blazepose", "openpose",
    ),
    "depth-estimation": (
        "midas", "dpt-large", "zoedepth",
    ),
    "image-text-retrieval": (
        "clip-vit", "openclip", "siglip",
    ),
    "embedding": (
        "bge-small", "bge-base", "bge-large",
        "e5-small", "e5-base", "e5-large",
        "gte-small", "gte-base",
        "instructor-xl", "jina-embed",
    ),
    "image-to-text": (
        "blip", "florence-2", "llava", "minicpm-v", "qwen-vl",
    ),
    "audio-enhancement": (
        "rnnoise", "dtln", "deepfilternet",
    ),
    "image-generation": (
        "sd-turbo", "sdxl-turbo", "stable-diffusion",
    ),
}

# Group-only fallbacks: keyword → group id. These intentionally stay
# coarse because the underlying tokens (vit / swin / diffusion / ...)
# collide across tasks, so the LLM (or a manual override) is expected
# to decide the task.
_GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "computer-vision": ("vit", "swin", "deit", "dino", "sam-base", "sam-large"),
    "generative-ai":   ("diffusion", "sdxl"),
    "multimodal":      ("vlm", "multimodal"),
    "audio":           ("audio", "speech", "sound"),
}


# Uniqueness check at module import — duplicate keywords across tasks
# would silently broken the rule layer; we crash immediately.
def _check_keyword_uniqueness() -> None:
    seen: dict[str, str] = {}
    for task, kws in _TASK_KEYWORDS.items():
        for kw in kws:
            if kw in seen and seen[kw] != task:
                raise RuntimeError(
                    "model_builder taxonomy: duplicate keyword "
                    f"{kw!r} in tasks {seen[kw]!r} and {task!r}"
                )
            seen[kw] = task


_check_keyword_uniqueness()


# Word-boundary regex for the rule matcher. Matches a token bordered
# by anything non-alphanumeric so ``mobilevit`` does not light up the
# group-only ``vit`` rule.
def _word_boundary_pattern(token: str) -> re.Pattern[str]:
    escaped = re.escape(token)
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


_TASK_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    task: tuple(_word_boundary_pattern(k) for k in kws)
    for task, kws in _TASK_KEYWORDS.items()
}
_GROUP_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    group: tuple(_word_boundary_pattern(k) for k in kws)
    for group, kws in _GROUP_KEYWORDS.items()
}


def _normalise_name(model_name: str) -> str:
    """Lowercase + collapse separators (mirrors legacy ``_normalize_name``)."""
    if not isinstance(model_name, str):
        return ""
    n = model_name.lower().strip()
    # Treat underscores as hyphens so ``real_esrgan`` and ``real-esrgan``
    # both match the ``real-esrgan`` keyword.
    n = n.replace("_", "-")
    n = re.sub(r"[\s/]+", "-", n)
    return n


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RuleAndShapeTaxonomyClassifier:
    """Rule + shape (+ optional LLM) classifier.

    The ``llm_callable`` is wired by DI when an LLM-backed
    classification path is desired. When ``None`` (the default) the
    classifier short-circuits to a group-only ``ClassifyResult`` with
    source ``"fallback"`` after the rule + shape passes both fail.
    """

    llm_callable: LLMClassifyCallable | None = None
    _supported_tasks: frozenset[str] = field(
        default_factory=lambda: frozenset(all_task_ids()),
        init=False,
        repr=False,
    )

    def classify(
        self,
        *,
        model_name: str,
        infer_manifest: dict[str, Any] | None = None,
    ) -> ClassifyResult:
        normalised = _normalise_name(model_name or "")

        # Layer 1: rule (task).
        for task, patterns in _TASK_PATTERNS.items():
            for p in patterns:
                if p.search(normalised):
                    group = group_of_task(task) or ""
                    return ClassifyResult(
                        group=group,
                        task=task,
                        source="rule",
                        confidence=0.95,
                    )

        # Layer 2: rule (group only).
        for group, patterns in _GROUP_PATTERNS.items():
            for p in patterns:
                if p.search(normalised):
                    return ClassifyResult(
                        group=group,
                        task=None,
                        source="rule",
                        confidence=0.6,
                    )

        # Layer 3: shape heuristic (best-effort from inference manifest).
        if infer_manifest:
            shape_result = self._classify_by_shape(infer_manifest)
            if shape_result is not None:
                return shape_result

        # Layer 4: optional LLM. The callable is responsible for
        # constraining its own response to the taxonomy enum; we still
        # validate before trusting the answer.
        if self.llm_callable is not None:
            try:
                llm_answer = self.llm_callable(
                    model_name,
                    self._build_constrained_prompt(),
                ) or ""
            except Exception:  # noqa: BLE001 — LLM transport unreliable
                llm_answer = ""
            llm_answer = llm_answer.strip().lower()
            if llm_answer in self._supported_tasks:
                return ClassifyResult(
                    group=group_of_task(llm_answer) or "",
                    task=llm_answer,
                    source="llm",
                    confidence=0.7,
                )

        # Final fallback: unclassified.
        return ClassifyResult(
            group="",
            task=None,
            source="fallback",
            confidence=0.0,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _classify_by_shape(
        self,
        infer_manifest: dict[str, Any],
    ) -> ClassifyResult | None:
        """Use input/output kind cues to narrow the classification.

        The legacy heuristic looked at ``inference_manifest.input.shape``
        and ``inference_manifest.output.type``. We replicate the cheap
        unambiguous cases — anything ambiguous falls through to the
        LLM / fallback layers.
        """
        try:
            input_block = infer_manifest.get("input") or {}
            output_block = infer_manifest.get("output") or {}
            input_kind = (
                (infer_manifest.get("input_kind"))
                or input_block.get("kind")
                or ""
            )
            output_kind = (
                (infer_manifest.get("output_kind"))
                or output_block.get("kind")
                or ""
            )
            output_type = (output_block.get("type") or "").lower()
        except (AttributeError, TypeError):
            return None

        # Image → image: two unambiguous tasks (super-resolution +
        # semantic segmentation share IO; we let the rule layer have
        # already handled the model-name signal, so a shape-only hit
        # at this point is a low-confidence "computer-vision" group).
        if input_kind == "image" and output_kind == "image":
            return ClassifyResult(
                group="computer-vision",
                task=None,
                source="shape",
                confidence=0.7,
            )

        # Audio → text: speech recognition.
        if input_kind == "audio" and output_kind == "text":
            return ClassifyResult(
                group="audio",
                task="speech-recognition",
                source="shape",
                confidence=0.85,
            )

        # Text → audio: text-to-speech.
        if input_kind == "text" and output_kind == "audio":
            return ClassifyResult(
                group="audio",
                task="audio-generation",
                source="shape",
                confidence=0.85,
            )

        # output.type sometimes carries the precise label.
        if output_type == "detection":
            return ClassifyResult(
                group="computer-vision",
                task="object-detection",
                source="shape",
                confidence=0.85,
            )
        if output_type == "segmentation":
            return ClassifyResult(
                group="computer-vision",
                task="semantic-segmentation",
                source="shape",
                confidence=0.85,
            )
        if output_type == "depth_estimation":
            return ClassifyResult(
                group="computer-vision",
                task="depth-estimation",
                source="shape",
                confidence=0.85,
            )

        # Audio classification when output is a 1-D vector and the
        # input is audio.
        if input_kind == "audio" and output_kind == "json":
            return ClassifyResult(
                group="audio",
                task="audio-classification",
                source="shape",
                confidence=0.5,
            )

        return None

    def _build_constrained_prompt(self) -> str:
        """Render the constrained-enum prompt suffix passed to the LLM.

        The legacy classifier injected the full task list to keep
        responses on-enum. We render a minimal version here — DI may
        replace this with a richer prompt by passing a closure that
        ignores the second argument.
        """
        tasks = ", ".join(sorted(self._supported_tasks))
        return (
            "Pick exactly one task id from this list (or the empty string "
            f"if uncertain): {tasks}"
        )


# ---------------------------------------------------------------------------
# IO inference helpers (used by QaiPackExporter when no override is set)
# ---------------------------------------------------------------------------

def io_kinds_for_classification(result: ClassifyResult) -> tuple[str, str]:
    """Map a :class:`ClassifyResult` to ``(input_kind, output_kind)``.

    Falls back to ``("image", "json")`` (legacy default) when the
    classifier could not narrow down to a task — matching the legacy
    ``infer_io_kinds`` behaviour for the empty / unknown case.
    """
    if result.task:
        io = io_for_task(result.task)
        if io:
            return io
    # Group-only or unclassified — pick the most common pair per group.
    by_group: dict[str, tuple[str, str]] = {
        "computer-vision": ("image", "json"),
        "audio":           ("audio", "audio"),
        "generative-ai":   ("text", "text"),
        "multimodal":      ("multi", "json"),
    }
    return by_group.get(result.group, ("image", "json"))
