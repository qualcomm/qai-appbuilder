# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Actionable diagnostics for a failed weight search.

"No usable .bin found" is a dead end: it tells neither the user nor an
agent what to do next. This module turns a failed search into evidence —
which directories were walked, which files were seen, why each was
rejected, and the concrete next step — so an LLM agent driving the
``model-builder`` / ``model-hub`` SKILL can diagnose and self-correct
instead of stalling.

The report is deliberately built from a *fresh, unfiltered* walk: it lists
weight-like files that the strict search rejected (too small, wrong
extension, nested deeper than expected), because those near-misses are
exactly what identifies the real problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .discovery import iter_tree_entries
from .precision import WEIGHT_SUFFIXES

__all__ = [
    "RejectedFile",
    "WeightSearchReport",
    "build_search_report",
]

#: Cap the listings so a huge tree cannot bloat an error message / prompt.
_MAX_LISTED = 25


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedFile:
    """A weight-like file that was found but not accepted."""

    path: str
    size_bytes: int
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightSearchReport:
    """Evidence describing why a weight search came up empty."""

    root: str
    root_exists: bool
    searched_recursively: bool
    #: Sub-directories seen under ``root`` (helps spot per-precision dirs).
    subdirs: tuple[str, ...] = field(default=())
    #: Weight-like files rejected, with the reason.
    rejected: tuple[RejectedFile, ...] = field(default=())
    #: Other file extensions present — reveals a conversion that stopped
    #: early (e.g. only ``.onnx`` / ``.dlc`` and no ``.bin``).
    other_extensions: tuple[str, ...] = field(default=())
    #: Precisions that WERE resolved, when the failure is precision-specific.
    found_precisions: tuple[str, ...] = field(default=())
    requested_precision: str | None = None

    def to_message(self) -> str:
        """Render a compact, agent-readable diagnostic block."""
        lines: list[str] = []
        if self.requested_precision:
            lines.append(
                f"No usable NPU weight for precision "
                f"{self.requested_precision!r} under {self.root}"
            )
        else:
            lines.append(f"No usable NPU weight found under {self.root}")

        if not self.root_exists:
            lines.append(f"  - directory does not exist: {self.root}")
            lines.append(
                "  next: verify the conversion completed and that the "
                "workspace path is correct"
            )
            return "\n".join(lines)

        scope = "recursively" if self.searched_recursively else "top-level only"
        lines.append(f"  searched: {scope}; accepted extensions: "
                     f"{', '.join(WEIGHT_SUFFIXES)}")

        if self.found_precisions:
            lines.append(
                f"  weights present for: {', '.join(self.found_precisions)}"
            )
        if self.subdirs:
            lines.append(f"  sub-directories: {', '.join(self.subdirs)}")
        if self.rejected:
            lines.append("  weight-like files rejected:")
            for item in self.rejected:
                lines.append(
                    f"    - {item.path} ({item.size_bytes:,} bytes) "
                    f"— {item.reason}"
                )
        if self.other_extensions:
            lines.append(
                f"  other extensions present: {', '.join(self.other_extensions)}"
            )
        if not self.rejected and not self.found_precisions:
            lines.append("  no .bin/.dlc files of any size were found")

        lines.append(f"  next: {self._next_step()}")
        return "\n".join(lines)

    def _next_step(self) -> str:
        """The single most useful action, inferred from the evidence."""
        if any(r.reason.startswith("too small") for r in self.rejected):
            return (
                "a 0-byte/stub weight means the real binary landed elsewhere "
                "— a shared --output_dir across precisions does this; re-run "
                "context-binary generation with a separate --output_dir per "
                "precision, or look for the real file in a nested directory"
            )
        if self.found_precisions and self.requested_precision:
            return (
                f"the requested precision is absent but "
                f"{', '.join(self.found_precisions)} exist — either export one "
                f"of those or re-run the pipeline with "
                f"--precision {self.requested_precision}"
            )
        if ".dlc" in self.other_extensions or ".onnx" in self.other_extensions:
            return (
                "conversion produced intermediate artefacts but no loadable "
                "weight — re-run the context-binary step (or export the .dlc, "
                "which QNNContext also loads)"
            )
        return (
            "run the conversion pipeline for this model, then confirm the "
            "weight is >= 1 MiB"
        )


def _classify(
    path: Path, *, min_bytes: int
) -> tuple[str, object] | None:
    """Bucket one filesystem entry for the report.

    Returns ``("dir", None)``, ``("other", suffix)``,
    ``("weight", (size, reason))`` or ``None`` to skip.
    """
    try:
        if path.is_dir():
            return ("dir", None)
        if not path.is_file():
            return None
        suffix = path.suffix.lower()
        if suffix not in WEIGHT_SUFFIXES:
            return ("other", suffix) if suffix else None
        size = path.stat().st_size
    except (OSError, ValueError):
        return None
    if size >= min_bytes:
        # Big enough, so it was skipped for another reason (precision
        # mismatch) — still worth surfacing as a near-miss.
        reason = "present but not selected (precision mismatch)"
    else:
        reason = f"too small (< {min_bytes:,} bytes — stub/placeholder)"
    return ("weight", (size, reason))


def build_search_report(
    root: Path,
    *,
    requested_precision: str | None = None,
    found_precisions: tuple[str, ...] = (),
    min_bytes: int,
    searched_recursively: bool = True,
) -> WeightSearchReport:
    """Walk ``root`` unfiltered and explain what blocked the search.

    Best-effort and side-effect free: any OS error degrades the report
    rather than raising, because this runs on an error path.
    """
    if not isinstance(root, Path):
        raise TypeError(f"root must be a Path, got {type(root).__name__}")
    try:
        root_is_dir = root.is_dir()
    except (OSError, ValueError):
        root_is_dir = False
    if not root_is_dir:
        return WeightSearchReport(
            root=str(root),
            root_exists=False,
            searched_recursively=searched_recursively,
            requested_precision=requested_precision,
        )

    subdirs: list[str] = []
    rejected: list[RejectedFile] = []
    others: set[str] = set()
    # Same depth-bounded, pruned walk ``discover_weights`` uses, so the report
    # describes exactly the tree that was searched — and cannot itself hang on
    # a caller-supplied path pointing at a huge directory.
    entries = sorted(iter_tree_entries(root), key=lambda p: str(p).lower())
    for path in entries:
        kind = _classify(path, min_bytes=min_bytes)
        if kind is None:
            continue
        bucket, payload = kind
        if bucket == "dir":
            if len(subdirs) < _MAX_LISTED:
                subdirs.append(path.relative_to(root).as_posix() + "/")
        elif bucket == "other":
            others.add(str(payload))
        elif len(rejected) < _MAX_LISTED:
            size, reason = payload  # type: ignore[misc]
            rejected.append(
                RejectedFile(
                    path=path.relative_to(root).as_posix(),
                    size_bytes=size,
                    reason=reason,
                )
            )

    return WeightSearchReport(
        root=str(root),
        root_exists=True,
        searched_recursively=searched_recursively,
        subdirs=tuple(subdirs),
        rejected=tuple(rejected),
        other_extensions=tuple(sorted(others)),
        found_precisions=found_precisions,
        requested_precision=requested_precision,
    )
