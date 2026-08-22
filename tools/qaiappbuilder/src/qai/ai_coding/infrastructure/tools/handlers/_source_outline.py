# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Structural outline of a source file — declarations only, bodies elided.

Why
---
Reading a 2000-line module to learn "what is in it" burns context for no
gain: the model needs the SHAPE (what classes/functions exist, their
signatures, where they start) and only then a narrow slice of one or two
bodies. Paging blindly through ``offset``/``limit`` is the expensive
alternative — several round-trips that each return mostly noise.

This module produces that shape. ``read`` with no explicit window on a
parseable source file returns the outline plus a footer naming the ranges
that were elided, so the follow-up read asks for exactly the lines needed.

Design
------
* **Python: real parsing.** ``ast`` (standard library) gives exact line
  spans for every declaration, so the outline never mis-attributes a
  nested ``def`` or a decorated method. No third-party dependency.
* **Other languages: line-anchored heuristics.** A small per-language
  regex set recognises declaration openers. This is deliberately shallow —
  the outline is a NAVIGATION aid, and a heuristic miss costs one extra
  read, not correctness. We never claim a body span we did not observe.
* **Fail open.** A syntax error, an unknown extension, or an unreadable
  file yields ``None`` so the caller falls back to a normal slice read. An
  outline is an optimisation; it must never be the reason a read fails.
* **Bodies are elided, never summarised.** We emit the declaration line and
  a marker naming the elided line range. Guessing at body content is how a
  model ends up reasoning over code that does not exist.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# Extensions whose declarations we can locate. Python gets a real parser;
# the rest are line-anchored (see module docstring).
_PY_SUFFIXES = frozenset({".py", ".pyi"})

#: Minimum line count before an outline is worth building. Below this the
#: whole file is cheaper to read than an outline plus a follow-up read.
MIN_OUTLINE_LINES = 60

#: Bodies shorter than this stay inline: eliding 2 lines to save 0 is noise.
_MIN_ELIDE_LINES = 3

#: Longest line the heuristic patterns are applied to. The declaration
#: regexes contain ambiguous whitespace/word alternations, so a very long
#: line of pure indentation or repeated keywords costs quadratic-or-worse
#: backtracking (measured: an 8 000-char indent run takes ~3 s on ONE
#: pattern). No real declaration opener is anywhere near this long, so a
#: longer line is skipped outright: it cannot be a declaration, and the
#: cap turns a pathological file (generated tables, minified sources,
#: column-aligned blobs) from a multi-minute stall into a no-op.
_MAX_HEURISTIC_LINE = 400

# Declaration openers for non-Python sources. Each pattern must anchor at
# the line start (after indentation) so a match inside a string or a
# trailing comment does not register as a declaration.
_HEURISTIC_PATTERNS: dict[frozenset[str], tuple[re.Pattern[str], ...]] = {
    frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}): (
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?"
            r"(?:class|interface|type|enum)\s+\w+"
        ),
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*\w+"
        ),
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*"
            r"(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>)"
        ),
        # Class members: `name(args) {` / `async name(args) {` / get/set.
        re.compile(
            r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+)*"
            r"(?:async\s+)?(?:get\s+|set\s+)?\w+\s*\([^;]*\)\s*\{?\s*$"
        ),
    ),
    frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx"}): (
        re.compile(r"^\s*(?:class|struct|union|enum|namespace)\s+\w+"),
        re.compile(r"^\s*template\s*<"),
        # Free / member function definition opening a brace block.
        re.compile(
            r"^[\w:<>,\s\*&~]+\s+[\w:~]+\s*\([^;]*\)\s*"
            r"(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\{?\s*$"
        ),
    ),
    frozenset({".rs"}): (
        re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl|mod|union)\s"),
        re.compile(r"^\s*(?:pub\s+)?(?:const\s+|unsafe\s+|async\s+)*fn\s+\w+"),
    ),
    frozenset({".go"}): (
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?\w+"),
        re.compile(r"^\s*type\s+\w+"),
    ),
    frozenset({".java", ".kt", ".cs"}): (
        re.compile(
            r"^\s*(?:public|private|protected|internal|open|sealed|data|"
            r"abstract|final|static|override|suspend|\s)*"
            r"(?:class|interface|enum|record|object|struct)\s+\w+"
        ),
        re.compile(
            r"^\s{2,}(?:public|private|protected|internal|static|final|"
            r"override|suspend|abstract|virtual|async|\s)*"
            r"[\w<>\[\],\.\?]+\s+\w+\s*\([^;]*\)\s*\{?\s*$"
        ),
    ),
}


def _patterns_for(suffix: str) -> tuple[re.Pattern[str], ...] | None:
    for suffixes, patterns in _HEURISTIC_PATTERNS.items():
        if suffix in suffixes:
            return patterns
    return None


def supports_outline(suffix: str) -> bool:
    """True when *suffix* names a language this module can outline."""
    lowered = suffix.lower()
    return lowered in _PY_SUFFIXES or _patterns_for(lowered) is not None


@dataclass(frozen=True, slots=True)
class _Decl:
    """One declaration: the line it opens on and the span it occupies."""

    start: int  # 1-indexed line of the declaration opener
    end: int  # 1-indexed last line of the whole declaration
    header_end: int  # last line of the signature (multi-line defs included)


def _python_decls(source: str) -> list[_Decl] | None:
    """Collect declaration spans via ``ast``; ``None`` when unparseable."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None

    decls: list[_Decl] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
            ):
                start = child.lineno
                # Decorators precede ``lineno`` and belong to the declaration.
                for deco in child.decorator_list:
                    start = min(start, deco.lineno)
                end = getattr(child, "end_lineno", None) or child.lineno
                # The signature can span lines; the body starts at the first
                # statement, so the header ends just before it.
                header_end = child.lineno
                if child.body:
                    first = child.body[0]
                    first_line = getattr(first, "lineno", header_end)
                    # A docstring is part of the useful header.
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        header_end = (
                            getattr(first, "end_lineno", first_line) or first_line
                        )
                    else:
                        header_end = max(child.lineno, first_line - 1)
                decls.append(_Decl(start=start, end=end, header_end=header_end))
                # Recurse so nested classes / methods are listed too.
                visit(child)

    visit(tree)
    decls.sort(key=lambda d: d.start)
    return decls


def _heuristic_decls(
    lines: list[str], patterns: tuple[re.Pattern[str], ...]
) -> list[_Decl]:
    """Locate declaration openers by line pattern (no body span known).

    The end of a declaration is taken as the line before the next
    declaration opener — an approximation that is adequate for navigation
    and never over-claims content (we only ever elide lines we counted).
    """
    starts = [
        idx + 1
        for idx, line in enumerate(lines)
        if line.strip()
        and len(line) <= _MAX_HEURISTIC_LINE
        and any(p.match(line) for p in patterns)
    ]
    decls: list[_Decl] = []
    for position, start in enumerate(starts):
        following = (
            starts[position + 1] - 1 if position + 1 < len(starts) else len(lines)
        )
        decls.append(_Decl(start=start, end=following, header_end=start))
    return decls


def split_source_lines(source: str) -> list[str]:
    """Split *source* the way the file reader and ``ast`` number lines.

    ``str.splitlines`` breaks on form feed, vertical tab, ``\\x1c``-``\\x1e``,
    ``U+2028``, ``U+0085`` and friends, but a text-mode file iteration (and
    therefore ``read``'s own line numbering, and ``ast``'s ``lineno``) breaks
    ONLY on ``\\n`` / ``\\r`` / ``\\r\\n``. On a file containing any of those
    extra characters ``splitlines`` yields MORE lines, so every outline line
    number after the first one drifts — the follow-up
    ``read(ranges=...)`` would then return the wrong lines. Universal-newline
    decoding has already normalised ``\\r\\n`` / ``\\r`` to ``\\n`` by the time
    the source string exists, so splitting on ``\\n`` alone is exact.
    """
    parts = source.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # trailing terminator does not open a new line
    return parts


def build_outline(source: str, suffix: str) -> tuple[str, int] | None:
    """Render a declaration outline for *source*.

    Returns ``(outline_text, elided_line_count)``, or ``None`` when the
    file is not worth outlining (too short, unsupported language,
    unparseable, or no declarations found). ``None`` means "fall back to a
    normal read" — the caller must always have that path.
    """
    lowered = suffix.lower()
    lines = split_source_lines(source)
    if len(lines) < MIN_OUTLINE_LINES:
        return None

    if lowered in _PY_SUFFIXES:
        decls = _python_decls(source)
        if decls is None:
            return None
    else:
        patterns = _patterns_for(lowered)
        if patterns is None:
            return None
        decls = _heuristic_decls(lines, patterns)

    if not decls:
        return None

    # Mark every line that must be SHOWN: declaration headers, plus module
    # level lines that are not inside any declaration body (imports,
    # constants, module docstring).
    show: list[bool] = [True] * (len(lines) + 1)  # 1-indexed
    total = len(lines)
    for decl in decls:
        body_start = decl.header_end + 1
        body_end = min(decl.end, total)
        if body_end - body_start + 1 < _MIN_ELIDE_LINES:
            continue
        for line_no in range(max(body_start, 1), body_end + 1):
            show[line_no] = False
    # Headers always win, even when nested inside another body. Runs as a
    # SECOND pass (not merged into the loop above) because a nested method's
    # header sits inside its enclosing class body: marking bodies first and
    # headers second makes the outcome independent of declaration order.
    for decl in decls:
        for line_no in range(max(decl.start, 1), min(decl.header_end, total) + 1):
            show[line_no] = True

    rendered: list[str] = []
    elided_total = 0
    run_start: int | None = None
    for line_no in range(1, len(lines) + 1):
        if show[line_no]:
            if run_start is not None:
                count = line_no - run_start
                elided_total += count
                rendered.append(
                    f"{run_start:>6}-{line_no - 1}: … {count} line(s) elided …"
                )
                run_start = None
            rendered.append(f"{line_no:>6}: {lines[line_no - 1]}")
        elif run_start is None:
            run_start = line_no
    if run_start is not None:
        count = len(lines) - run_start + 1
        elided_total += count
        rendered.append(f"{run_start:>6}-{len(lines)}: … {count} line(s) elided …")

    if elided_total == 0:
        # Nothing was hidden, so the outline is just the file — no gain.
        return None
    return "\n".join(rendered), elided_total
