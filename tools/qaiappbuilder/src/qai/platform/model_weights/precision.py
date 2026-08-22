# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Precision-token normalisation — the single source of truth.

Both bounded contexts that touch NPU weights need to agree on what a
precision token means:

* ``qai.model_builder`` — writes ``output/<model>_<label>.bin`` and, on
  export, must map a requested precision back onto a file on disk;
* ``qai.app_builder`` — scans a workspace and shows the user one row per
  precision in the import wizard.

Historically each context carried its OWN table and they drifted: the
import side learned ``bf16`` / ``w16a16`` while the export side did not,
so a ``bf16`` build showed up in the wizard and then failed to export
with "no usable .bin". Keeping the table here (shared kernel, importable
by every context per the ``context-isolation`` contract) makes drift
structurally impossible.

Design rules
------------
1. **Never let an unknown token drop a weight file.** Discovery is done
   by file EXTENSION (see :mod:`qai.platform.model_weights.discovery`);
   the precision is metadata attached afterwards.
2. **Never let an unknown token get a WRONG label either.** "Don't drop
   the file" is not a licence to lie about it. A declared-but-unrecognised
   precision (MeloTTS ships ``precision: "mixed_with_float"``) is
   PRESERVED as its own key by :func:`normalise_precision` and displayed
   as ``MIXED_WITH_FLOAT``. :data:`DEFAULT_PRECISION` covers only the
   *absence* of information — ``None``, blank, or a filename with no
   identifiable precision segment.
3. **Accept generated bit-width labels.** ``run_pipeline.py`` builds its
   label as ``f"w{weight_bw}a{act_bw}"`` (plus an optional ``b{bias_bw}``)
   from free-form ``--act_bw`` / ``--weight_bw`` flags, so the token space
   is open-ended. :func:`normalise_precision` therefore recognises the
   ``w<N>a<N>[b<N>]`` SHAPE via regex instead of enumerating every combo.
4. **A declaration is not a guess.** A ``precision`` field in a manifest
   or plan states a fact; a ``_``-separated filename segment is inferred.
   Loose natural-language aliases (``float``) are honoured only in the
   former — see :data:`_DECLARATION_ONLY_ALIASES`.
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_PRECISION",
    "KNOWN_PRECISION_LABELS",
    "WEIGHT_SUFFIXES",
    "display_label",
    "is_known_precision_token",
    "label_for",
    "normalise_precision",
    "precision_from_filename",
    "precision_segment_index",
]

#: Precision assumed when a weight file carries no recognisable suffix.
#: Model Builder's own default is fp16 (``run_pipeline.py`` ``--precision``
#: defaults to fp16) and AI Hub float packages are fp16, so this is the
#: safe, non-lossy fallback.
DEFAULT_PRECISION = "fp16"

#: NPU weight extensions the app_pack contract accepts. Format-neutral:
#: ``QNNContext`` loads a QNN context binary (``.bin``) or a QNN DLC
#: (``.dlc``) directly.
WEIGHT_SUFFIXES: tuple[str, ...] = (".bin", ".dlc")

# Label/alias form (as it appears in filenames and plans) → plan-form key.
_ALIAS_TO_PLAN: dict[str, str] = {
    # ── Float ──
    "fp16": "fp16",
    "fp32": "fp32",
    "float": "fp32",  # an LLM agent may write PRECISION=float in plan.md
    "bf16": "bf16",
    # ── Quantised: label form ──
    "int8": "w8a8",  # canonical for INT8 outputs (ahead of w8a8b8)
    "int4": "w4a8",
    # ── Quantised: plan form ──
    "w8a8": "w8a8",
    "w8a8b8": "w8a8b8",
    "w8a16": "w8a16",
    "w4a8": "w4a8",
    "w4a16": "w4a16",
    "w16a16": "w16a16",
}

#: Aliases valid ONLY in a DECLARED precision field (``plan.md``'s
#: ``PRECISION=``, ``inference_manifest.json``'s ``precision``) — never as a
#: filename segment.
#:
#: ``float`` is the whole reason this set exists. It is a legitimate spelling
#: when an agent *declares* it, but ``_``-segment scanning made it fire inside
#: compound words: ``melotts_zh_mixed_with_float`` ends in the segment
#: ``float``, so the heuristic reported ``("fp32", True)`` — confidently
#: mislabelling a 293 MB mixed-precision weight as FP32. A declaration is a
#: statement of fact; a filename segment is a guess. Only the former may lean
#: on loose natural-language aliases.
_DECLARATION_ONLY_ALIASES: frozenset[str] = frozenset({"float"})

# Plan-form key → filename label used by the conversion pipeline.
_PLAN_TO_LABEL: dict[str, str] = {
    "fp16": "fp16",
    "fp32": "fp32",
    "bf16": "bf16",
    "w8a8": "int8",
    "w8a8b8": "int8",
    "w8a16": "w8a16",
    "w4a8": "int4",
    "w4a16": "w4a16",
    "w16a16": "w16a16",
}

# Plan-form key → human-facing label shown in the import picker.
_PLAN_TO_DISPLAY: dict[str, str] = {
    "fp16": "FP16",
    "fp32": "FP32",
    "bf16": "BF16",
    "w8a8": "INT8",
    "w8a8b8": "INT8",
    "w8a16": "W8A16",
    "w4a8": "INT4",
    "w4a16": "W4A16",
    "w16a16": "W16A16",
}

#: Every explicitly known alias (filename label or plan form).
KNOWN_PRECISION_LABELS: tuple[str, ...] = tuple(sorted(_ALIAS_TO_PLAN))

# ``w8a16`` / ``w4a8b8`` / ``w16a16`` … — the open-ended shape emitted by
# ``run_pipeline.py`` for custom ``--act_bw`` / ``--weight_bw`` runs.
_BITWIDTH_RE = re.compile(r"^w(?P<w>\d{1,2})a(?P<a>\d{1,2})(?:b(?P<b>\d{1,2}))?$")

# An unrecognised token is preserved verbatim only when it *looks like* a
# precision identifier: starts with a letter, then identifier-ish characters,
# and short enough to render in a picker cell. The guard exists so a corrupt
# manifest ("<html>…", a 4 KB blob, a stray newline) degrades to the default
# instead of being echoed into the UI as a precision name.
_PRESERVABLE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_+.-]{0,31}$")


def normalise_precision(token: str | None) -> str:
    """Return the plan-form precision for a DECLARED ``token``.

    For declared precision fields — ``plan.md``'s ``PRECISION=``,
    ``inference_manifest.json``'s ``precision``. Recognises, in order: the
    alias table (including declaration-only spellings such as ``float``), the
    generated ``w<N>a<N>[b<N>]`` shape, then **preserves** any remaining
    identifier-shaped token as its own precision key.

    Preservation is the point. A pipeline that declares
    ``precision: "mixed_with_float"`` means exactly that; folding it into
    :data:`DEFAULT_PRECISION` used to make the import picker announce "FP16"
    for a weight that is not fp16. "Never drop the file" was always the rule —
    it never licensed printing a precision the weight does not have.

    :data:`DEFAULT_PRECISION` is therefore reserved for *absence of
    information*: ``None``, a non-string, a blank string, or a value too
    malformed to be a precision name at all (see
    :data:`_PRESERVABLE_TOKEN_RE`). Contrast
    :func:`precision_from_filename`, which also defaults when a *filename*
    carries no recognisable token — there the segment holding the precision is
    unidentifiable, so there is nothing to preserve.
    """
    if not isinstance(token, str):
        return DEFAULT_PRECISION
    t = token.strip().lower()
    if not t:
        return DEFAULT_PRECISION
    plan = _ALIAS_TO_PLAN.get(t)
    if plan is not None:
        return plan
    if _BITWIDTH_RE.match(t):
        # Preserve the generated token verbatim: it IS the plan form.
        return t
    if _PRESERVABLE_TOKEN_RE.match(t):
        # Unknown but well-formed — keep the declaration honest.
        return t
    return DEFAULT_PRECISION


def is_known_precision_token(token: str | None) -> bool:
    """Return ``True`` when ``token`` is a precision spelling we RECOGNISE.

    Narrower than :func:`normalise_precision` in two ways, both deliberate:

    * an unknown-but-preserved token (``mixed_with_float``) is NOT known — it
      is data we pass through, not a precision we can reason about;
    * :data:`_DECLARATION_ONLY_ALIASES` (``float``) are NOT known here, because
      this function drives the *filename* heuristic
      (:func:`precision_segment_index`). ``melotts_zh_mixed_with_float`` must
      not have its trailing ``float`` segment read as a precision claim.

    Callers use it to decide whether a name carried real precision information
    or a default was substituted.
    """
    if not isinstance(token, str):
        return False
    t = token.strip().lower()
    if not t or t in _DECLARATION_ONLY_ALIASES:
        return False
    return t in _ALIAS_TO_PLAN or bool(_BITWIDTH_RE.match(t))


def label_for(plan_key: str) -> str:
    """Filename label for a plan-form key (``w8a8`` → ``int8``)."""
    return _PLAN_TO_LABEL.get(plan_key, plan_key)


def display_label(plan_key: str) -> str:
    """Human-facing label for the import picker (``w8a8`` → ``INT8``)."""
    known = _PLAN_TO_DISPLAY.get(plan_key)
    if known is not None:
        return known
    return plan_key.upper() if plan_key else DEFAULT_PRECISION.upper()


def precision_from_filename(stem: str) -> tuple[str, bool]:
    """Infer the precision from a weight file's stem.

    Scans the ``_``-separated segments from the RIGHT and takes the first
    recognised precision token. Scanning every segment (not just the last
    one) matters because real filenames carry trailing qualifiers:
    ``resnet50_w8a16_final.bin`` and ``yolo_int8_quantized.bin`` would
    otherwise inspect only ``final`` / ``quantized``, fail, and silently be
    labelled ``fp16`` — mislabelling a quantised weight as float and making
    it compete for the fp16 slot. Right-to-left keeps the precision-bearing
    segment closest to the extension authoritative when a stem mentions
    several (``a_fp16_b_int8`` → ``int8``).

    Deliberately does NOT require the stem to be prefixed by the model or
    workspace name: real workspaces disagree (workspace ``yolov8`` holding
    ``yolov8n_fp16.bin``), and the prefix carries no precision signal.

    A leading underscore (``_fp16.bin``) or a bare precision name
    (``fp16.bin``) counts too — the token matters, not the punctuation.

    :data:`_DECLARATION_ONLY_ALIASES` are excluded here (see
    :func:`is_known_precision_token`): a filename segment is a *guess*, so it
    may only match tight, unambiguous spellings. ``float`` matching the tail of
    ``melotts_zh_mixed_with_float`` is precisely the false positive that made
    a mixed-precision weight report FP32.

    Returns ``(plan_key, explicit)`` where ``explicit`` is ``True`` when the
    filename really did carry a recognised precision token and ``False`` when
    :data:`DEFAULT_PRECISION` was substituted.

    Note how this differs from :func:`normalise_precision`, which PRESERVES an
    unknown token. The two fallbacks answer different questions:

    * a filename with no recognised segment tells us nothing about *which*
      segment (if any) is the precision — ``melotts_zh_mixed_with_float`` has
      six candidates and no way to choose — so the only honest answer is "no
      information", i.e. the default flagged ``explicit=False``;
    * a declared ``precision`` field IS the precision, whatever it spells, so
      :func:`normalise_precision` keeps it verbatim.
    """
    index = precision_segment_index(stem)
    if index is None:
        return DEFAULT_PRECISION, False
    segments = stem.split("_")
    return normalise_precision(segments[index]), True


def precision_segment_index(stem: str) -> int | None:
    """Index of the ``_``-separated segment of ``stem`` holding the precision.

    Returns ``None`` when no segment is a recognised precision token. Shared
    with weight discovery so the model-name derivation strips exactly the
    segment this function identified (no second, diverging split).
    """
    if not isinstance(stem, str) or not stem:
        return None
    segments = stem.split("_")
    for index in range(len(segments) - 1, -1, -1):
        if is_known_precision_token(segments[index]):
            return index
    return None
