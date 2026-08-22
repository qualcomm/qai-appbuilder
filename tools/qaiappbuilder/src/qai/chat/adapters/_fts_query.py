# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared FTS5 query helpers for chat full-text search adapters.

Extracted so both :class:`SqliteConversationRepository` (sidebar search)
and :class:`SqliteConversationSearchRepository` (the ``search_conversations``
tool) build identical FTS5 ``MATCH`` expressions and ``<mark>`` snippets —
one source of truth for CJK bigram handling and HTML escaping.

CJK handling
------------
The ``unicode61`` tokenizer treats each CJK character as its own token, so
a multi-char Chinese query must be split into overlapping **bigrams** to
match contiguous runs (e.g. ``天气预报`` -> ``"天气" "气预" "预报"``). Each
term is double-quoted (inner double-quotes doubled) so FTS5 syntactic
characters (``-`` ``*`` ``(`` ``:`` ...) in user input are treated
literally rather than as operators.
"""

from __future__ import annotations

__all__ = ["build_fts_match", "escape_html", "make_snippet"]

#: A multi-char CJK query is split into overlapping bigrams; a single CJK
#: char (or non-CJK) is not. This is the threshold for bigram splitting.
_MIN_CJK_FOR_BIGRAM = 2


def build_fts_match(query: str) -> str:
    """Build an FTS5 ``MATCH`` expression from a raw user query.

    Multi-char CJK queries become overlapping bigrams; other queries split
    on whitespace. Every term is double-quoted so user-supplied FTS5
    syntactic characters are literal, not operators.
    """
    cjk = [c for c in query if "\u4e00" <= c <= "\u9fff"]
    if len(cjk) >= _MIN_CJK_FOR_BIGRAM:
        terms = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    else:
        # Non-CJK (or single CJK char): split on whitespace, drop empties.
        terms = [t for t in query.split() if t] or [query]
    quoted = [f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in terms]
    return " ".join(quoted)


def make_snippet(text: str, query: str, *, context: int = 32) -> str:
    """Build a ``<mark>``-highlighted excerpt for a LIKE fallback path.

    Mirrors the *shape* of FTS5 ``snippet(..., '<mark>', '</mark>',
    '...', 32)``: a window of up to ``context`` characters on either side
    of the first case-insensitive match, matched substring wrapped in
    ``<mark>...</mark>`` and ``...`` ellipses where the text was clipped.
    HTML-special characters in the surrounding text are escaped.
    """
    if not text or not query:
        return ""
    lower_text = text.lower()
    lower_q = query.lower()
    idx = lower_text.find(lower_q)
    if idx < 0:
        return ""
    start = max(0, idx - context)
    end = min(len(text), idx + len(query) + context)
    before = escape_html(text[start:idx])
    match = escape_html(text[idx : idx + len(query)])
    after = escape_html(text[idx + len(query) : end])
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{before}<mark>{match}</mark>{after}{suffix}"


def escape_html(value: str) -> str:
    """Minimal HTML escape (``&`` ``<`` ``>``) for snippet text segments."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
