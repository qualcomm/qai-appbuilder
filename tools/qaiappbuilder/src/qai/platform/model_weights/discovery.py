# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""NPU weight discovery — extension-driven, never precision-driven.

The rule this module exists to enforce: **find weights by file extension,
then attach precision as metadata.** Filtering the filesystem walk by a
precision table loses files whenever the table and the converter disagree
— and they do disagree in practice, because ``run_pipeline.py`` accepts
free-form ``--act_bw`` / ``--weight_bw`` flags and the SKILL steers agents
toward ``bf16`` when quantisation misses its accuracy gate.

So :func:`discover_weights` enumerates every ``.bin`` / ``.dlc`` at or
below a root, and each result carries a precision resolved by
:func:`qai.platform.model_weights.precision.precision_from_filename`,
defaulting to ``fp16`` when the name says nothing. Nothing is dropped for
being unrecognised.

Recursion matters: multi-precision context-binary generation is documented
to use a separate ``--output_dir`` per precision (``model-builder``'s
``references/context_binary.md``), which puts real weights at
``output/<precision>/<model>_<precision>.bin``. A flat scan misses all of
them; a shared-``output_dir`` run instead leaves 0-byte placeholders at the
top level with the real binary under ``bins/``. Recursing plus the size
floor handles both shapes.

Enumeration is not the whole story, though: a discovered file is a
*candidate*, not a precision. Multi-component models export several
unlabelled ``.bin`` pieces into one directory, and treating each as its own
precision made a 28 MB ``flow.bin`` fragment win the FP16 slot in the import
picker. :func:`partition_component_weights` separates real precision
variants from a model's parts before anything is offered to a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .precision import (
    WEIGHT_SUFFIXES,
    display_label,
    precision_from_filename,
    precision_segment_index,
)

__all__ = [
    "MAX_WEIGHT_SEARCH_DEPTH",
    "MIN_WEIGHT_BYTES",
    "PRUNED_DIR_NAMES",
    "WeightCandidate",
    "discover_weights",
    "iter_tree_entries",
    "partition_component_weights",
    "select_best_per_precision",
]

#: Size floor for a usable weight (1 MiB). Anything smaller is a stub or a
#: 0-byte placeholder from a shared-``output_dir`` context-binary run; such a
#: file would crash AppBuilder on first inference.
MIN_WEIGHT_BYTES = 1 * 1024 * 1024

#: How deep below ``root`` to walk. Real layouts need 2 at most —
#: ``output/<model>_<prec>.bin`` (depth 1) and the per-precision or ``bins/``
#: nesting that multi-precision context-binary generation produces (depth 2).
#: The cap exists because ``model_workdir`` arrives from the caller: pointing
#: it at a large tree would otherwise walk the whole thing, which is exactly
#: the 30-minute hang the model-hub SKILL forbids (Issue 18). A weight buried
#: deeper than this is not a pipeline product.
MAX_WEIGHT_SEARCH_DEPTH = 3

#: Directory names never worth descending into. Dependency / VCS / cache trees
#: hold no NPU weights but can dominate the walk when a workspace happens to
#: sit beside them.
PRUNED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".cache",
        ".idea",
        ".vscode",
        "site-packages",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightCandidate:
    """One discovered NPU weight file plus its resolved metadata."""

    path: Path
    size_bytes: int
    mtime: float
    #: Plan-form precision (``fp16`` / ``w8a8`` / ``bf16`` / ...).
    precision: str
    #: Human-facing label for pickers (``FP16`` / ``INT8`` / ...).
    label: str
    #: ``True`` when the filename actually carried a recognised precision
    #: token; ``False`` when the ``fp16`` default was substituted.
    precision_explicit: bool
    #: Filename stem minus the precision suffix — informational only. The
    #: model name is NEVER required to match the workspace directory name.
    stem_model_name: str

    @property
    def is_context_binary(self) -> bool:
        """``True`` for a QNN context binary (``.bin``), ``False`` for a DLC."""
        return self.path.suffix.lower() == ".bin"


def _stem_model_name(stem: str) -> str:
    """Model name carried by ``stem``, with the precision segment removed.

    Informational only — the model name is NEVER a matching criterion (a
    workspace named ``yolov8`` legitimately holds ``yolov8n_fp16.bin``).
    Reuses :func:`precision_segment_index` so the segment dropped here is
    exactly the one the precision was read from: a naive ``rfind("_")`` would
    turn ``resnet50_w8a16_final`` into ``resnet50_w8a16``, leaving the
    precision inside the model name.
    """
    index = precision_segment_index(stem)
    if index is None:
        return stem
    segments = stem.split("_")
    kept = segments[:index] + segments[index + 1 :]
    return "_".join(part for part in kept if part) or stem


def iter_tree_entries(
    root: Path, *, max_depth: int = MAX_WEIGHT_SEARCH_DEPTH
) -> list[Path]:
    """Every entry (files AND directories) at or below ``root``, depth-bounded.

    Shared with the diagnostics builder so the report describes exactly the
    tree :func:`discover_weights` searched — same depth cap, same pruning,
    same symlink policy. Never raises; unreadable subtrees are skipped.
    """
    out: list[Path] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        current, depth = pending.pop(0)
        try:
            entries = list(current.iterdir())
        except (OSError, ValueError):
            continue
        for entry in entries:
            out.append(entry)
            try:
                if not entry.is_dir() or depth >= max_depth:
                    continue
                if entry.name.lower() in PRUNED_DIR_NAMES:
                    continue
                if entry.is_symlink():
                    continue
                pending.append((entry, depth + 1))
            except (OSError, ValueError):
                continue
    return out


def _iter_weight_paths(
    root: Path, *, recursive: bool, max_depth: int
) -> list[Path]:
    """Collect weight-suffixed files at or below ``root``, depth-bounded.

    Hand-rolled breadth-first walk instead of ``Path.rglob`` because ``rglob``
    is unbounded: ``model_workdir`` comes from the caller, so an unlucky path
    would walk an entire drive (the 30-minute hang the model-hub SKILL forbids,
    Issue 18). Also prunes dependency/VCS/cache directories and skips symlinked
    directories, which is where link cycles would otherwise send the walk.

    Errors are swallowed per-directory (a permission-denied subtree must not
    void the whole scan) and the traversal never raises.
    """
    found: list[Path] = []
    # (directory, depth-below-root); depth 0 is ``root`` itself.
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        current, depth = pending.pop(0)
        try:
            entries = list(current.iterdir())
        except (OSError, ValueError):
            continue  # unreadable subtree — keep whatever else we can see
        for entry in entries:
            try:
                if entry.is_dir():
                    if not recursive or depth >= max_depth:
                        continue
                    if entry.name.lower() in PRUNED_DIR_NAMES:
                        continue
                    if entry.is_symlink():
                        continue  # never follow links: cycle guard
                    pending.append((entry, depth + 1))
                elif entry.suffix.lower() in WEIGHT_SUFFIXES:
                    found.append(entry)
            except (OSError, ValueError):
                continue  # stat/type probe failed for this entry only
    return found


def discover_weights(
    root: Path,
    *,
    recursive: bool = True,
    min_bytes: int = MIN_WEIGHT_BYTES,
    max_depth: int = MAX_WEIGHT_SEARCH_DEPTH,
) -> tuple[WeightCandidate, ...]:
    """Enumerate every usable NPU weight at or below ``root``.

    Selection is by extension (:data:`WEIGHT_SUFFIXES`) and size only — no
    precision filtering, so a weight is never lost to a naming mismatch. The
    precision is attached afterwards as metadata (unlabelled → ``fp16``).

    Args:
        root: directory to walk (typically ``<workspace>/output``).
        recursive: walk sub-directories (default). ``False`` lists only ``root``.
        min_bytes: reject files smaller than this (placeholder guard).
        max_depth: how many levels below ``root`` to descend
            (:data:`MAX_WEIGHT_SEARCH_DEPTH`); bounds the walk so a caller
            pointing at a large tree cannot hang the request.

    Returns:
        Candidates sorted by path for deterministic output. A missing or
        unreadable ``root`` yields an empty tuple rather than raising —
        discovery is best-effort by design.
    """
    if not isinstance(root, Path):
        raise TypeError(f"root must be a Path, got {type(root).__name__}")
    try:
        if not root.is_dir():
            return ()
    except (OSError, ValueError):
        return ()

    paths = sorted(
        _iter_weight_paths(root, recursive=recursive, max_depth=max_depth),
        key=lambda p: str(p).lower(),
    )

    out: list[WeightCandidate] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_size < min_bytes:
                continue
        except (OSError, ValueError):
            continue
        precision, explicit = precision_from_filename(path.stem)
        out.append(
            WeightCandidate(
                path=path,
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
                precision=precision,
                label=display_label(precision),
                precision_explicit=explicit,
                stem_model_name=_stem_model_name(path.stem),
            )
        )
    return tuple(out)


def select_best_per_precision(
    candidates: tuple[WeightCandidate, ...] | list[WeightCandidate],
) -> dict[str, WeightCandidate]:
    """Collapse ``candidates`` to one weight per precision.

    App Builder needs exactly ONE weight per precision — several files can
    resolve to the same one (a ``_int8.bin`` beside a ``_w8a8.bin``, or the
    same precision as both ``.bin`` and ``.dlc``), and emitting duplicates
    produced a confusing picker with checkboxes toggling in lockstep.

    Ranking, in order:

    1. an explicit precision suffix beats a defaulted one — a real
       ``_fp16.bin`` outranks an unlabelled file that merely fell back to
       ``fp16``;
    2. ``.bin`` (native QNN context binary) beats ``.dlc``;
    3. newer mtime;
    4. larger file — the tie-break that discards a stub kept beside a real
       binary of the same name.
    """
    best: dict[str, WeightCandidate] = {}
    for cand in candidates:
        current = best.get(cand.precision)
        if current is None or _rank(cand) > _rank(current):
            best[cand.precision] = cand
    return best


def _rank(cand: WeightCandidate) -> tuple[int, int, float, int]:
    return (
        1 if cand.precision_explicit else 0,
        1 if cand.is_context_binary else 0,
        cand.mtime,
        cand.size_bytes,
    )


def _component_rank(cand: WeightCandidate, *, name_key: str) -> tuple[int, int, float]:
    """Rank an unlabelled candidate's claim to BE the model (vs. be a part).

    Name signal first, size second — in that order deliberately. Size alone is
    not a sound rule: MeloTTS' fused ``melotts_zh_mixed_with_float.bin`` happens
    to be the largest file, but a decoder-heavy export need not be, and
    hard-coding "biggest wins" would silently promote a fragment the moment
    that stops holding. The model-name prefix is a *semantic* signal — the
    pipeline names its product after the model and its parts after their
    function (``encoder`` / ``flow`` / ``decoder``) — so it decides first, and
    size only breaks ties among files with the same standing.
    """
    return (
        1 if name_key and _normalise_name(cand.stem_model_name).startswith(name_key) else 0,
        cand.size_bytes,
        cand.mtime,
    )


def _normalise_name(name: str) -> str:
    """Fold a model/stem name for prefix comparison.

    ``melotts_zh`` (workspace) vs. ``melotts_zh_mixed_with_float`` (file) vs.
    ``melotts-zh`` (a hub id) must all compare equal at the prefix, so
    separators and case are dropped.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def partition_component_weights(
    candidates: tuple[WeightCandidate, ...] | list[WeightCandidate],
    *,
    model_name: str,
) -> tuple[tuple[WeightCandidate, ...], tuple[WeightCandidate, ...]]:
    """Split ``candidates`` into (precision variants, auxiliary components).

    **A precision is a set of files, not one file per file.** A multi-component
    model exports its pieces side by side — MeloTTS drops ``bert_normalizer``,
    ``bert_zh_tokenizer``, ``encoder``, ``flow``, ``decoder`` and the fused
    weight into one ``output/``, and the built-in pack manifest confirms the
    intent by listing them together in a single variant's
    ``runtime.contextBins``. Treating each as its own precision candidate meant
    all five fell to the ``fp16`` default and the biggest fragment
    (``flow.bin``, 28 MB) won the FP16 slot: the import picker offered a
    precision that did not exist, pointing at a file that cannot run alone.

    The discriminator is the *explicit* flag, not the file list:

    * a weight whose name carries a recognised precision token
      (``model_w8a8.bin``, ``model_bf16.dlc``) is a precision CLAIM — always a
      variant, never demoted;
    * unlabelled weights are grouped **per directory**, because that is the
      unit an export writes: a per-precision sub-directory layout
      (``output/fp16/model.bin``, ``output/w8a16/model.bin``) holds one weight
      each and must survive, while several unlabelled weights in the SAME
      directory are far more likely one model's parts than several precisions
      that all forgot to name themselves. A lone unlabelled weight is the
      model (``output/yolov8n.dlc``), so single-file groups pass through
      untouched.

    Within a multi-file group, one representative is promoted (see
    :func:`_component_rank`) and the rest are returned as auxiliary. Auxiliary
    weights are *demoted, not hidden*: they are handed back so callers can keep
    them visible (the diagnostics report walks the tree unfiltered), which
    matters because a wrong guess here must stay diagnosable.

    Args:
        candidates: output of :func:`discover_weights`.
        model_name: workspace / model name used as the naming signal. May be
            empty, in which case size decides.

    Returns:
        ``(variants, auxiliary)`` — ``variants`` preserves input order so the
        caller's deterministic path sort is not disturbed.
    """
    name_key = _normalise_name(model_name) if isinstance(model_name, str) else ""

    by_dir: dict[Path, list[WeightCandidate]] = {}
    for cand in candidates:
        if cand.precision_explicit:
            continue
        by_dir.setdefault(cand.path.parent, []).append(cand)

    demoted: set[Path] = set()
    for group in by_dir.values():
        if len(group) < 2:
            continue  # a lone unlabelled weight IS the model
        primary = max(group, key=lambda c: _component_rank(c, name_key=name_key))
        demoted.update(c.path for c in group if c.path != primary.path)

    if not demoted:
        return tuple(candidates), ()
    variants = tuple(c for c in candidates if c.path not in demoted)
    auxiliary = tuple(c for c in candidates if c.path in demoted)
    return variants, auxiliary
