# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Reference ledger for compaction-aware wire injection.

Pure domain object: tracks a bounded, deduplicated set of file paths,
URLs, and recent shell commands that appeared in a conversation's tool
calls.  A rendered "``[References Ledger]``" block is injected into the
wire alongside a compaction checkpoint so the model still knows what
artifacts have been touched even after the raw tool traffic has been
dropped.

Design constraints:

* stdlib + ``dataclasses`` only -- **no** adapter / application layer
  dependencies.  Extractors (`streaming`, MCP heuristics, ...) produce
  :class:`Reference` instances and hand them to :meth:`ReferenceLedger.add`.
* Deterministic mode merging (see :meth:`ReferenceLedger.add`) so ledger
  content is stable across runs.
* Deterministic capacity policy: files/urls/execs each have a hard cap;
  eviction prefers read-only entries when a mix exists, otherwise
  falls back to FIFO on insertion order.
* Total rendered block is capped at 500 bytes; anything beyond is
  truncated with a ``... N more elided`` marker so wire cost is bounded.
* Sibling of :mod:`qai.chat.domain.content` in spirit -- pure value
  semantics, no I/O, no logging.

See ``docs/90-refactor/CONTEXT-COMPRESSION-NEXT.md`` §5.2 for the
end-to-end design and §5.2.4 / §5.2.8 for the selector and render
formats respectively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar, Literal

ReferenceKind = Literal["file", "url", "exec"]
FileMode = Literal["R", "W", "RW"]

# --------------------------------------------------------------------- #
# Selector stripping (§5.2.4)
# --------------------------------------------------------------------- #

# Matches a numeric range list: 50 / 50-100 / 50+30 / 5,10-20 / 5,10-20,30-40
RANGE_LIST_SRC = r"\d+(?:[-+]\d+)?(?:,\d+(?:[-+]\d+)?)*"

# Numeric range with optional ``:raw`` prefix or suffix (``:raw:2-4`` / ``:2-4:raw``).
_RANGE_SELECTOR_RE = re.compile(rf":(?:raw:)?{RANGE_LIST_SRC}(?::raw)?$")

# Trailing bare ``:raw`` marker (no numeric range).
_READ_RAW_ONLY_RE = re.compile(r":raw$")


def strip_read_selector(path: str) -> str:
    """Strip line-range selectors and ``:raw`` markers from a read path.

    Examples::

        foo.py:50-100    -> foo.py
        foo.py:50+30     -> foo.py
        foo.py:5,10-20   -> foo.py
        foo.py:raw       -> foo.py
        foo.py:2-4:raw   -> foo.py
        foo.py:raw:2-4   -> foo.py
        foo.py           -> foo.py

    Non-numeric selectors (``foo.py:not-a-range``) are left untouched --
    only real line-range / raw markers are recognised.  An empty string
    is safe (returns ``""``).
    """
    if not path:
        return path
    path = _RANGE_SELECTOR_RE.sub("", path)
    path = _READ_RAW_ONLY_RE.sub("", path)
    return path


# --------------------------------------------------------------------- #
# Mode merging (§5.2.3 note)
# --------------------------------------------------------------------- #

# (existing, incoming) -> merged.  W never downgrades to R; RW absorbs
# everything; R+W upgrades to RW.
_MODE_MERGE: dict[tuple[FileMode, FileMode], FileMode] = {
    ("R", "R"): "R",
    ("R", "W"): "RW",
    ("R", "RW"): "RW",
    ("W", "R"): "W",
    ("W", "W"): "W",
    ("W", "RW"): "RW",
    ("RW", "R"): "RW",
    ("RW", "W"): "RW",
    ("RW", "RW"): "RW",
}


def _merge_modes(existing: FileMode, incoming: FileMode) -> FileMode:
    return _MODE_MERGE[(existing, incoming)]


# --------------------------------------------------------------------- #
# Reference value + ledger
# --------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Reference:
    """One extracted reference from a tool call.

    ``value`` is the raw path / URL / command as observed at the call
    site.  Callers are NOT required to strip line-range selectors --
    the ledger does that on ingest so all origins converge on a single
    canonical form.
    """

    kind: ReferenceKind
    value: str
    mode: FileMode = "R"


@dataclass(slots=True)
class ReferenceLedger:
    """Bounded, deduplicated ledger of file / URL / command references.

    ``files`` is an insertion-ordered ``dict`` (Python 3.7+ guarantees
    order) keyed by canonicalised path, mapped to the current combined
    :data:`FileMode`.  ``urls`` is likewise an insertion-ordered
    ``dict`` (with ``None`` values) so eviction can drop the oldest
    entry when the cap is hit.  ``execs`` is an LRU-style list where
    repeating an already-recorded command bumps it to the tail.
    """

    files: dict[str, FileMode] = field(default_factory=dict)
    # NB: an insertion-ordered dict (values ignored) so we can evict the
    # oldest URL when :data:`MAX_URLS` is hit.  Externally callers should
    # treat this as a set of URLs.
    urls: dict[str, None] = field(default_factory=dict)
    execs: list[str] = field(default_factory=list)

    MAX_FILES: ClassVar[int] = 20
    MAX_URLS: ClassVar[int] = 10
    MAX_EXECS: ClassVar[int] = 5

    # Hard byte-budget for the rendered wire block (§5.2.3).
    MAX_WIRE_BYTES: ClassVar[int] = 500

    # ----------------------------------------------------------------- #
    # Mutation
    # ----------------------------------------------------------------- #

    def add(self, ref: Reference) -> None:
        """Incorporate a new reference.

        * ``kind="file"``: canonicalise via :func:`strip_read_selector`,
          then merge modes according to :func:`_merge_modes`.  Adding
          an already-present path re-enters it at the *original*
          position (so mode upgrades don't reset LRU ordering), but a
          brand-new path lands at the tail.  When ``MAX_FILES`` is
          exceeded, evict the oldest read-only (``"R"``) entry; if no
          ``"R"`` remains, drop the oldest entry unconditionally.
        * ``kind="url"``: insert into an ordered set; on overflow drop
          the oldest URL.
        * ``kind="exec"``: LRU append -- if the command is already
          present, remove the old occurrence and re-append so the most
          recent invocation sits at the tail.  Trim to
          :data:`MAX_EXECS` from the head (FIFO).
        """
        if ref.kind == "file":
            self._add_file(ref)
        elif ref.kind == "url":
            self._add_url(ref.value)
        elif ref.kind == "exec":
            self._add_exec(ref.value)
        # Unknown kinds silently ignored -- domain object stays permissive.

    def _add_file(self, ref: Reference) -> None:
        path = strip_read_selector(ref.value)
        if not path:
            return
        existing = self.files.get(path)
        if existing is not None:
            # Merge modes in place; keep the entry at its current
            # insertion slot so mode upgrades don't churn LRU order.
            self.files[path] = _merge_modes(existing, ref.mode)
            return

        self.files[path] = ref.mode
        self._enforce_files_cap()

    def _add_url(self, url: str) -> None:
        if not url:
            return
        # Re-inserting an existing URL keeps its original position; only
        # a brand-new URL bumps the eviction candidate.
        if url in self.urls:
            return
        self.urls[url] = None
        while len(self.urls) > self.MAX_URLS:
            oldest = next(iter(self.urls))
            del self.urls[oldest]

    def _add_exec(self, cmd: str) -> None:
        if not cmd:
            return
        # LRU: remove old occurrence, append at tail.
        try:
            self.execs.remove(cmd)
        except ValueError:
            pass
        self.execs.append(cmd)
        # FIFO trim from the head so we retain the most recent commands.
        while len(self.execs) > self.MAX_EXECS:
            self.execs.pop(0)

    def _enforce_files_cap(self) -> None:
        """Evict oldest entries until ``len(files) <= MAX_FILES``.

        Prefers dropping read-only entries first (they are cheapest to
        forget: nothing has been mutated on our behalf).  Falls back to
        FIFO when no ``"R"`` entry remains.
        """
        while len(self.files) > self.MAX_FILES:
            victim: str | None = None
            for path, mode in self.files.items():
                if mode == "R":
                    victim = path
                    break
            if victim is None:
                victim = next(iter(self.files))
            del self.files[victim]

    # ----------------------------------------------------------------- #
    # Composition
    # ----------------------------------------------------------------- #

    def merge(self, other: ReferenceLedger) -> None:
        """Fold another ledger in via the standard :meth:`add` semantics.

        Used to accumulate references across successive compactions.
        """
        for path, mode in other.files.items():
            self.add(Reference(kind="file", value=path, mode=mode))
        for url in other.urls:
            self.add(Reference(kind="url", value=url))
        for cmd in other.execs:
            self.add(Reference(kind="exec", value=cmd))

    # ----------------------------------------------------------------- #
    # Serialization
    # ----------------------------------------------------------------- #

    def to_json(self) -> dict:
        """Serialise to a JSON-friendly ``dict`` for checkpoint storage.

        Order is preserved (files / urls / execs iterate in insertion
        order) so a round-trip through :meth:`from_json` yields an
        identical ledger.
        """
        return {
            "files": dict(self.files),
            "urls": list(self.urls),
            "execs": list(self.execs),
        }

    @classmethod
    def from_json(cls, data: dict | None) -> ReferenceLedger | None:
        """Reconstruct a ledger from :meth:`to_json` output.

        ``None`` / empty maps back to ``None`` so callers can express
        the "no ledger recorded" state (which matches the ``NULL``
        column of pre-migration rows -- see §5.2.5).
        """
        if not data:
            return None
        files_raw = data.get("files") or {}
        urls_raw = data.get("urls") or []
        execs_raw = data.get("execs") or []

        files: dict[str, FileMode] = {}
        for path, mode in files_raw.items():
            if mode in ("R", "W", "RW"):
                files[str(path)] = mode  # type: ignore[assignment]

        urls: dict[str, None] = {}
        for url in urls_raw:
            if isinstance(url, str):
                urls[url] = None

        execs: list[str] = [cmd for cmd in execs_raw if isinstance(cmd, str)]

        return cls(files=files, urls=urls, execs=execs)

    # ----------------------------------------------------------------- #
    # Predicates
    # ----------------------------------------------------------------- #

    def is_empty(self) -> bool:
        return not self.files and not self.urls and not self.execs

    # ----------------------------------------------------------------- #
    # Wire rendering (§5.2.8)
    # ----------------------------------------------------------------- #

    def render_wire_block(self) -> str | None:
        """Render the ledger as a ``[References Ledger]`` block.

        Returns ``None`` when the ledger is empty so callers can skip
        wire injection entirely.  When the natural rendering exceeds
        :data:`MAX_WIRE_BYTES`, entries are dropped from the tail and
        replaced with a ``... N more elided`` marker so the final
        UTF-8 length always fits under the cap.
        """
        if self.is_empty():
            return None

        # Build a flat list of "column entries" so truncation can pick a
        # coherent unit.  Each entry is (label, value); ``label`` is
        # empty for continuation rows aligned under the first entry of
        # its group.
        entries: list[tuple[str, str]] = []

        r_files = [p for p, m in self.files.items() if m == "R"]
        w_files = [p for p, m in self.files.items() if m == "W"]
        rw_files = [p for p, m in self.files.items() if m == "RW"]

        def _push(label: str, values: list[str]) -> None:
            for i, value in enumerate(values):
                entries.append((label if i == 0 else "", value))

        _push("Files (R):", r_files)
        _push("Files (W):", w_files)
        _push("Files (RW):", rw_files)
        _push("URLs:", list(self.urls))
        _push("Recent Exec:", list(self.execs))

        header = "[References Ledger]"
        return _render_bounded(header, entries, self.MAX_WIRE_BYTES)


# --------------------------------------------------------------------- #
# Internal rendering helpers
# --------------------------------------------------------------------- #

# Width of the label column when a real label is present.  Chosen so
# common labels ("Files (RW):", "Recent Exec:") line up without wasting
# bytes on padding for the shorter ones.
_LABEL_WIDTH = 12


def _format_row(label: str, value: str) -> str:
    if label:
        return f"{label:<{_LABEL_WIDTH}} {value}"
    return f"{' ' * _LABEL_WIDTH} {value}"


def _render_bounded(header: str, entries: list[tuple[str, str]], budget: int) -> str:
    """Assemble ``header`` + rows, truncating so the UTF-8 length ≤ ``budget``.

    Truncation drops entries from the tail; the number dropped is
    surfaced via a ``... N more elided`` marker (also budgeted).
    """
    rows = [_format_row(label, value) for label, value in entries]
    body = "\n".join([header, *rows])
    if len(body.encode("utf-8")) <= budget:
        return body

    # Binary-ish shrink: drop from the tail until header + rows + marker
    # fits.  ``kept`` is the number of rendered rows retained.
    kept = len(rows)
    while kept > 0:
        elided = len(rows) - kept
        marker = f"... {elided} more elided"
        candidate = "\n".join([header, *rows[:kept], marker])
        if len(candidate.encode("utf-8")) <= budget:
            return candidate
        kept -= 1

    # No single row fits; return header + marker alone (guaranteed tiny).
    return "\n".join([header, f"... {len(rows)} more elided"])


__all__ = [
    "FileMode",
    "RANGE_LIST_SRC",
    "Reference",
    "ReferenceKind",
    "ReferenceLedger",
    "strip_read_selector",
]
