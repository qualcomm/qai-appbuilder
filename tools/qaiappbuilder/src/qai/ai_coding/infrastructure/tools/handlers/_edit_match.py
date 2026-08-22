# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Pure matching/replacement helpers for the ``edit`` tool.

The ``edit`` tool finds ``oldText`` in a file and replaces it. A naive exact
``str.count`` / ``str.replace`` fails on two very common, harmless mismatches
between what the model emits and what is on disk:

* **Line endings.** A file saved on Windows uses ``\\r\\n``; a model almost
  always emits ``\\n`` in its ``oldText``. An exact match then finds nothing and
  the edit fails for no real reason. We normalise both sides to ``\\n`` for
  matching and restore the file's own line ending on write.
* **Whitespace / indentation drift.** A model frequently gets the indentation
  or trailing whitespace of ``oldText`` slightly wrong. A short ladder of
  progressively more lenient matchers recovers these "off by a space" cases
  while still REFUSING to guess when a match is ambiguous (more than one
  candidate) — so an edit never silently lands in the wrong place.

Everything here is a pure function over strings (no filesystem, no I/O) so it is
trivially unit-testable and reused by the ``edit`` handler. The handler keeps
all the FileGuard / protected-path / async-thread plumbing; this module only
decides *where* a replacement goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "EditMatchError",
    "ReplaceResult",
    "detect_line_ending",
    "normalize_newlines",
    "restore_line_ending",
    "replace_block",
]


class EditMatchError(Exception):
    """Raised when ``oldText`` cannot be matched, or matches ambiguously.

    Carries a model-friendly ``message`` explaining the failure so the ``edit``
    handler can surface it verbatim as a tool error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ReplaceResult:
    """Outcome of a single replacement against newline-normalised content.

    ``consumed`` is the TOTAL length (in characters of newline-normalised
    content) of the original region(s) removed by this replacement. It is the
    crux of the pre-write CONSERVATION check in ``tool_edit``:

        len(content_after) == len(content_before) - consumed
                              + len(new) * replacements

    For the single-match strategies ``replacements == 1`` and ``consumed`` is
    the actual matched region length (which is NOT always ``len(old)``: the
    fuzzy strategies — ``line_trim`` / ``block_trim`` / ``indentation_flexible``
    / ``whitespace_normalized`` / ``block_anchor`` / ``escape_normalized`` —
    match a region whose length differs from the literal ``old``). For
    ``replace_all`` (``exact_all``)
    every occurrence is an EXACT match of ``old``, so ``consumed == len(old) *
    replacements`` and the equation collapses to the usual
    ``+ (len(new) - len(old)) * replacements``.
    """

    content: str  # the new content (newline-normalised; restore EOL on write)
    replacements: int  # how many occurrences were replaced
    strategy: str  # which matcher succeeded (for diagnostics / tests)
    consumed: int  # total chars of ORIGINAL content removed by this replace


def detect_line_ending(text: str) -> str:
    """Return the file's dominant line ending (``\\r\\n`` if any CRLF present).

    A single CRLF anywhere classifies the file as CRLF (Windows convention):
    mixed-ending files are rare and treating them as CRLF preserves the more
    structured ending on write. Files with no line ending at all default to
    ``\\n``.
    """
    return "\r\n" if "\r\n" in text else "\n"


def normalize_newlines(text: str) -> str:
    """Collapse ``\\r\\n`` and lone ``\\r`` to ``\\n`` for ending-insensitive
    matching."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_ending(text: str, line_ending: str) -> str:
    """Convert ``\\n``-normalised ``text`` back to ``line_ending`` for writing.

    No-op when ``line_ending`` is ``\\n``.
    """
    if line_ending == "\n":
        return text
    return text.replace("\n", line_ending)


def _find_unique(haystack: str, needle: str) -> int | None:
    """Return the single occurrence index of ``needle`` in ``haystack``.

    Returns ``None`` when there is no occurrence; raises :class:`EditMatchError`
    when there is more than one (ambiguous — the caller must add context).
    """
    first = haystack.find(needle)
    if first == -1:
        return None
    second = haystack.find(needle, first + 1)
    if second != -1:
        return None  # ambiguous at this strategy level — let caller decide
    return first


def _count(haystack: str, needle: str) -> int:
    return haystack.count(needle)


def _strategy_exact(content: str, old: str) -> tuple[int, int] | None:
    """Exact substring match. Returns (index, match_len) or None."""
    idx = _find_unique(content, old)
    if idx is None:
        return None
    return idx, len(old)


def _strategy_trim(content: str, old: str) -> tuple[int, int] | None:
    """Match ignoring leading/trailing whitespace of the WHOLE block.

    Handles the common case where the model included or dropped a blank line /
    surrounding spaces around an otherwise exact block.
    """
    stripped = old.strip()
    if not stripped or stripped == old:
        return None
    idx = _find_unique(content, stripped)
    if idx is None:
        return None
    return idx, len(stripped)


def _strategy_line_trim(content: str, old: str) -> tuple[int, int] | None:
    """Match line-by-line ignoring each line's leading/trailing whitespace.

    Recovers indentation / trailing-space drift: the model's block matches a
    region of the file when every line is compared with its surrounding
    whitespace stripped. The matched region in the ORIGINAL content (with its
    real indentation) is what gets replaced, so the surrounding code keeps its
    formatting.
    """
    old_lines = [ln.strip() for ln in old.split("\n")]
    # Drop a trailing empty element from a block that ended in "\n".
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    if not old_lines:
        return None

    content_lines = content.split("\n")
    n = len(old_lines)
    matches: list[int] = []  # start line indices of candidate regions
    for start in range(0, len(content_lines) - n + 1):
        window = content_lines[start : start + n]
        if [ln.strip() for ln in window] == old_lines:
            matches.append(start)
    if len(matches) != 1:
        return None  # 0 = no match, >1 = ambiguous

    start = matches[0]
    # Compute the character span of these lines in the original content. We
    # match the line CONTENT only and deliberately EXCLUDE the trailing newline
    # after the final matched line, so a newText that does not itself end in a
    # newline does not swallow the line break (the model usually omits the
    # trailing EOL of the block it is replacing).
    char_start = sum(len(ln) + 1 for ln in content_lines[:start])
    region_lines = content_lines[start : start + n]
    # Bytes of the lines plus the newline BETWEEN them (n-1 separators).
    region_len = sum(len(ln) for ln in region_lines) + (n - 1)
    return char_start, region_len


def _line_window_span(content_lines: list[str], start: int, n: int) -> tuple[int, int]:
    """Compute the (char_start, region_len) span of a ``n``-line window.

    The span covers ``content_lines[start : start + n]`` INCLUDING the ``n - 1``
    newlines BETWEEN those lines but EXCLUDING the trailing newline after the
    final line — identical to how ``_strategy_line_trim`` computes its span, so
    every window-based fuzzy strategy consumes exactly the real character region
    of the matched lines (this is what makes the caller's conservation check
    ``len_after == len_before - consumed + len(new)`` hold: ``consumed`` is the
    true removed-region length, never ``len(old)``).
    """
    char_start = sum(len(ln) + 1 for ln in content_lines[:start])
    region_lines = content_lines[start : start + n]
    region_len = sum(len(ln) for ln in region_lines) + (n - 1)
    return char_start, region_len


def _remove_common_indent(lines: list[str]) -> list[str]:
    """Strip the common minimum leading whitespace from every non-blank line.

    Blank lines are left untouched. Preserves the RELATIVE indentation between
    lines while discarding the block's overall indentation level.
    """
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return lines
    min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
    if min_indent == 0:
        return lines
    return [ln if not ln.strip() else ln[min_indent:] for ln in lines]


def _strategy_indentation_flexible(content: str, old: str) -> tuple[int, int] | None:
    """Match ignoring the block's overall indentation level.

    Both ``old`` and each candidate window are de-indented (common minimum
    leading whitespace removed) before comparison, so a block the model emitted
    at the wrong indentation depth — but with correct relative indentation —
    still matches. Returns the ORIGINAL (real-indentation) window's char span.
    """
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    n = len(old_lines)
    if n == 0:
        return None

    normalized_old = _remove_common_indent(old_lines)
    content_lines = content.split("\n")
    matches: list[int] = []
    for start in range(0, len(content_lines) - n + 1):
        window = content_lines[start : start + n]
        if _remove_common_indent(window) == normalized_old:
            matches.append(start)
    if len(matches) != 1:
        return None
    return _line_window_span(content_lines, matches[0], n)


def _normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) to a single space, then
    strip the ends."""
    return re.sub(r"\s+", " ", text).strip()


def _strategy_whitespace_normalized(content: str, old: str) -> tuple[int, int] | None:
    """Match ignoring intra-block whitespace differences.

    Both ``old`` and each candidate window (same line count as ``old``) are
    whitespace-normalised (all whitespace runs → single space, ends stripped)
    before comparison, recovering "spaces got squished / expanded" drift.
    Returns the ORIGINAL window's char span so the real region is consumed.
    """
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    n = len(old_lines)
    if n == 0:
        return None

    normalized_old = _normalize_ws("\n".join(old_lines))
    if not normalized_old:
        return None
    content_lines = content.split("\n")
    matches: list[int] = []
    for start in range(0, len(content_lines) - n + 1):
        window = content_lines[start : start + n]
        if _normalize_ws("\n".join(window)) == normalized_old:
            matches.append(start)
    if len(matches) != 1:
        return None
    return _line_window_span(content_lines, matches[0], n)


# Fraction of middle lines that must match (after strip) for a block-anchor
# candidate to be accepted. Kept simple (no Levenshtein) — a plain equal-line
# ratio — to stay pure-Python with no dependencies.
_BLOCK_ANCHOR_MIN_SIMILARITY = 0.5


def _strategy_block_anchor(content: str, old: str) -> tuple[int, int] | None:
    """Match a multi-line block by its first + last line (anchors).

    Only used for blocks of >= 3 lines. Finds windows whose first and last
    (stripped) lines equal ``old``'s first/last (stripped) lines AND whose line
    count equals ``old``'s. Among those, keeps only candidates whose middle
    lines are at least ``_BLOCK_ANCHOR_MIN_SIMILARITY`` similar (fraction of
    stripped middle lines equal). Commits ONLY when exactly one such candidate
    survives (never guesses). Returns that window's real char span.
    """
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    n = len(old_lines)
    if n < 3:
        return None

    first_anchor = old_lines[0].strip()
    last_anchor = old_lines[-1].strip()
    old_middle = [ln.strip() for ln in old_lines[1:-1]]

    content_lines = content.split("\n")
    matches: list[int] = []
    for start in range(0, len(content_lines) - n + 1):
        window = content_lines[start : start + n]
        if window[0].strip() != first_anchor or window[-1].strip() != last_anchor:
            continue
        win_middle = [ln.strip() for ln in window[1:-1]]
        if not old_middle:
            # No middle lines: anchors alone qualify the candidate.
            matches.append(start)
            continue
        same = sum(1 for a, b in zip(old_middle, win_middle) if a == b)
        if same / len(old_middle) >= _BLOCK_ANCHOR_MIN_SIMILARITY:
            matches.append(start)
    if len(matches) != 1:
        return None
    return _line_window_span(content_lines, matches[0], n)


# Literal escape sequences (a backslash char followed by a letter/symbol) that
# a model sometimes emits instead of the real control characters on disk.
_ESCAPE_MAP = {
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
}


def _unescape_literal(text: str) -> str:
    """Turn literal ``\\n`` / ``\\t`` / ``\\r`` (backslash + letter) into the
    real control characters."""
    result = text
    for literal, real in _ESCAPE_MAP.items():
        result = result.replace(literal, real)
    return result


def _strategy_escape_normalized(content: str, old: str) -> tuple[int, int] | None:
    """Match after un-escaping literal ``\\n`` / ``\\t`` / ``\\r`` in ``old``.

    A model occasionally emits ``oldText`` with two-character escape sequences
    (``\\n``) where the file has the real control character. Un-escape ``old``
    then do a unique exact find in the ORIGINAL content, so the span is a real
    contiguous character region.
    """
    unescaped = _unescape_literal(old)
    if unescaped == old:
        return None  # nothing to unescape — exact strategy already tried this
    idx = _find_unique(content, unescaped)
    if idx is None:
        return None
    return idx, len(unescaped)


# Matcher ladder: exact first, then progressively more lenient. Each returns a
# (index, match_len) span in ``content`` or ``None``. The ladder STOPS at the
# first strategy that yields exactly one unambiguous span.
#
# ``line_trim`` is tried before ``block_trim``: line-trim replaces whole matched
# LINES (so the original region's exact byte span — including its real
# indentation — is what gets swapped out), which is the safer recovery for a
# single indented line. ``block_trim`` strips the block's outer whitespace and
# does a substring find, which is the right fallback for a multi-line block that
# differs only in surrounding blank lines but can mis-indent a single line, so
# it sits last.
#
# After the original three, four progressively more aggressive fuzzy matchers
# are appended. They all reuse
# ``_line_window_span`` (or a unique exact find) so ``match_len`` is ALWAYS the
# real character span of the matched region — never ``len(old)`` — keeping the
# caller's conservation check valid. Each commits ONLY on a single unambiguous
# candidate and returns ``None`` (skip, do not guess) otherwise:
#   * ``indentation_flexible`` — de-indent block, match relative indentation.
#   * ``whitespace_normalized`` — collapse all whitespace runs, match squished
#     spacing.
#   * ``block_anchor`` — first/last line anchors + middle-line similarity
#     (>= 3 lines only).
#   * ``escape_normalized`` — un-escape literal ``\\n`` / ``\\t`` / ``\\r`` then
#     exact find.
_STRATEGIES = (
    ("exact", _strategy_exact),
    ("line_trim", _strategy_line_trim),
    ("block_trim", _strategy_trim),
    ("indentation_flexible", _strategy_indentation_flexible),
    ("whitespace_normalized", _strategy_whitespace_normalized),
    ("block_anchor", _strategy_block_anchor),
    ("escape_normalized", _strategy_escape_normalized),
)


def replace_block(
    content: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
) -> ReplaceResult:
    """Replace ``old`` with ``new`` in ``content`` (newline-normalised).

    ``content`` / ``old`` / ``new`` must already be ``\\n``-normalised. Tries the
    matcher ladder; the FIRST strategy yielding a single unambiguous match wins.

    * ``replace_all=True`` replaces EVERY exact occurrence (only the exact
      strategy is used — fuzzy "replace all" would be too risky). Requires at
      least one occurrence.
    * ``replace_all=False`` requires a UNIQUE match. An exact match found in more
      than one place raises :class:`EditMatchError` asking for more context
      (a fuzzy ladder strategy that is itself ambiguous is skipped, not fatal).

    Raises :class:`EditMatchError` when nothing matches at any strategy level.
    """
    if replace_all:
        occurrences = _count(content, old)
        if occurrences == 0:
            raise EditMatchError(
                "oldText not found in file.\n"
                f"  Expected: {old[:120]!r}"
            )
        return ReplaceResult(
            content=content.replace(old, new),
            replacements=occurrences,
            strategy="exact_all",
            # Every occurrence is an EXACT match of ``old``, so the total
            # consumed length is ``len(old) * occurrences``.
            consumed=len(old) * occurrences,
        )

    # Unique-match path. Exact first: if it appears more than once that is a
    # hard, model-actionable error (add surrounding context) — do NOT fall
    # through to a fuzzy strategy that might pick a different single region.
    exact_count = _count(content, old)
    if exact_count > 1:
        raise EditMatchError(
            f"oldText matches {exact_count} locations (must be unique).\n"
            f"  Text: {old[:120]!r}\n"
            "  Add surrounding lines to oldText so it identifies one region, "
            "or set replaceAll to replace every occurrence."
        )

    for name, strategy in _STRATEGIES:
        span = strategy(content, old)
        if span is None:
            continue
        idx, match_len = span
        return ReplaceResult(
            content=content[:idx] + new + content[idx + match_len :],
            replacements=1,
            strategy=name,
            # ``match_len`` IS the length of the original region removed —
            # ``len(old)`` for the exact strategy, but the actual matched
            # span for the fuzzy strategies (line_trim / block_trim /
            # indentation_flexible / whitespace_normalized / block_anchor /
            # escape_normalized), which can match a region longer/shorter
            # than the literal old.
            consumed=match_len,
        )

    raise EditMatchError(
        "oldText not found in file (tried exact and whitespace-tolerant "
        "matching).\n"
        f"  Expected: {old[:120]!r}"
    )
