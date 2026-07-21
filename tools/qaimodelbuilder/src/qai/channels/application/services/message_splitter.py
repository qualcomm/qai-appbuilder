# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""WeChat 5KB long-message splitter (PR-203).

Personal WeChat caps each outbound message at 5000 characters.  When
the LLM produces a long answer the channel sync layer splits it into
chunks via :class:`MessageSplitter` before handing them to the
:class:`ChannelTransportPort.send_text` adapter.

The splitter is intentionally:

* **Stateless** — no module-level globals, no caches.
* **Pure** — same input always yields the same output.
* **Lossless** — ``"".join(splitter.split(text)) == text`` for every
  input.

Boundary preference (highest-preference first) when a chunk needs to
be cut short of ``max_chars``:

1. ``"\\n\\n"`` — paragraph break.
2. ``"\\n"`` — single newline.
3. Sentence-end punctuation: ``! ? .`` (ASCII) and ``。 ！ ？`` (CJK).
4. Western punctuation followed by space: ``", "``, ``"! "``, ``"? "``.
5. Single space.
6. Arbitrary character boundary (last resort) — Python ``str`` is
   code-point indexed so this never splits inside a surrogate pair on
   normal Unicode input.
"""

from __future__ import annotations

__all__ = ["MessageSplitter"]


# Boundary tokens, evaluated in order.  Each entry is a tuple
# ``(needle, length)`` and we look for the *last* occurrence of needle
# inside the candidate window — the cut happens **after** the needle so
# the chunk preceding the cut keeps the boundary character(s).
#
# Sentence-end + space and bare sentence-end live as separate tiers
# because "Hello." standing alone is a stronger boundary than ".." in
# the middle of an ellipsis-heavy run.
_PARAGRAPH_BREAK = "\n\n"
_NEWLINE = "\n"
_CJK_SENTENCE_ENDS: tuple[str, ...] = ("。", "！", "？")
_WESTERN_SENTENCE_END_SPACE: tuple[str, ...] = (". ", "! ", "? ")
_BARE_SENTENCE_ENDS: tuple[str, ...] = (".", "!", "?")
_SPACE = " "


class MessageSplitter:
    """Split long outbound text into <= ``max_chars`` chunks.

    Stateless / pure.  Default ``max_chars`` is 5000 (the personal-
    WeChat per-message cap from legacy
    ``backend/channels/wechat/sender.py``).
    """

    DEFAULT_MAX_CHARS = 5000

    __slots__ = ("_max_chars",)

    def __init__(self, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        self._max_chars = max_chars

    @property
    def max_chars(self) -> int:
        return self._max_chars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def split(self, text: str) -> list[str]:
        """Split ``text`` into <= ``max_chars`` chunks.

        Returns ``[]`` for empty input and ``[text]`` for any input
        already short enough.  The concatenation of the returned list
        equals the original text byte-for-byte (lossless).
        """
        if not text:
            return []
        if len(text) <= self._max_chars:
            return [text]

        chunks: list[str] = []
        pos = 0
        n = len(text)
        while pos < n:
            remaining = n - pos
            if remaining <= self._max_chars:
                chunks.append(text[pos:])
                break

            # Candidate window: text[pos : pos + max_chars].  We need to
            # find the rightmost preferred boundary inside it.  ``cut``
            # is an absolute index into ``text``.
            window_end = pos + self._max_chars
            cut = self._find_cut(text, pos, window_end)
            chunks.append(text[pos:cut])
            pos = cut
        return chunks

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _find_cut(self, text: str, start: int, window_end: int) -> int:
        """Locate the absolute index at which to cut the chunk.

        ``window_end`` is the exclusive upper bound (``start + max_chars``).
        The returned index is in ``(start, window_end]`` — strictly
        greater than ``start`` (forward progress) and at most
        ``window_end`` (chunk size cap).
        """
        # Tier 1: paragraph break ("\n\n").  Cut just after it.
        idx = text.rfind(_PARAGRAPH_BREAK, start, window_end)
        if idx != -1:
            cut = idx + len(_PARAGRAPH_BREAK)
            if cut > start:
                return cut

        # Tier 2: single newline.  Cut just after it.
        idx = text.rfind(_NEWLINE, start, window_end)
        if idx != -1:
            cut = idx + 1
            if cut > start:
                return cut

        # Tier 3: CJK sentence-end punctuation.
        cut = self._rfind_any_after(text, _CJK_SENTENCE_ENDS, start, window_end)
        if cut > start:
            return cut

        # Tier 4: western sentence-end + space (".", "! ", "? ").
        cut = self._rfind_any_after(
            text, _WESTERN_SENTENCE_END_SPACE, start, window_end
        )
        if cut > start:
            return cut

        # Tier 4b: bare western sentence-end punctuation (".", "!", "?").
        cut = self._rfind_any_after(text, _BARE_SENTENCE_ENDS, start, window_end)
        if cut > start:
            return cut

        # Tier 5: single space.
        idx = text.rfind(_SPACE, start, window_end)
        if idx != -1:
            cut = idx + 1
            if cut > start:
                return cut

        # Tier 6: arbitrary cut at window_end.  Python ``str`` is
        # code-point indexed so this is safe for normal Unicode (the
        # only way to land mid-surrogate is if the source string
        # contains lone surrogates, which we don't attempt to repair).
        return window_end

    @staticmethod
    def _rfind_any_after(
        text: str,
        needles: tuple[str, ...],
        start: int,
        window_end: int,
    ) -> int:
        """Return the highest ``index + len(needle)`` for any needle in
        ``needles`` found within ``text[start:window_end]``.

        Returns ``-1`` (sentinel: no match) when none of the needles
        appear in the window.  Callers must treat ``cut <= start`` as
        "no usable boundary" and fall through to the next tier.
        """
        best = -1
        for needle in needles:
            idx = text.rfind(needle, start, window_end)
            if idx == -1:
                continue
            cut = idx + len(needle)
            if cut > best:
                best = cut
        return best
