# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Text normalization helpers shared across bounded contexts.

Currently provides:
  - :func:`strip_ansi_escapes`: remove ANSI/VT100 escape sequences from
    captured stdout/stderr before returning the text to an LLM or rendering
    it in a tool card.

Why ANSI stripping matters for tool output (LLM context):
  Many CLI programs emit color/cursor escape codes via the ``colorama``,
  ``colorlog``, ``rich``, ``tqdm``, etc. libraries. On Windows, when the
  program's stdout is piped to a child process (which is what subprocess /
  ``asyncio.create_subprocess_shell`` does), these libraries often FAIL to
  detect the redirect and still emit codes like ``\x1b[32m...\x1b[0m``.

  Literal escape bytes in tool output confuse a downstream LLM (no terminal):
    1. Models mistake ``[32m`` for content boundaries / markup.
    2. Tokens are wasted on meaningless escape bytes.
    3. grep/match reasoning fails because the literal text the model expects
       (e.g. ``[INFO]``) is now prefixed with ``\x1b[32m``.

  Color codes have NO semantic value to a downstream LLM (which renders no
  terminal). Stripping them makes tool output purely textual and matches
  what the user would see in a properly-detected non-tty mode.

Layering note (§3.2 / §3.5 import-linter):
  This module lives in ``qai.platform`` (the shared kernel), so every bounded
  context may import it (``context-isolation`` contract whitelists
  ``qai.** -> qai.platform.**``). It is a pure function with no I/O, no
  framework dependency.

Compatibility note:
  The regex targets the most common CSI (Control Sequence Introducer) family
  - ``ESC [ ... <final byte>`` - which covers SGR (color), cursor movement,
  erase, and most other VT100/ANSI sequences. It also covers the rarer OSC
  (``ESC ]...BEL`` or ``ESC ]...ESC \\``) used by tools like ripgrep for
  hyperlink output, plus other short 7-bit ESC sequences.
"""

from __future__ import annotations

import re

__all__ = ["strip_ansi_escapes"]


# CSI sequences: ESC [ <params> <intermediate> <final 0x40-0x7E>
# Plus OSC sequences: ESC ] <text> (ST | BEL)
# Plus other short 7-bit ESC sequences: ESC = ESC > ESC c ...
#
# NOTE: this VERBOSE pattern uses an explicit alternation group `(?: ... )`
# so the inline `#` comments don't get parsed inside character classes.
_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b                # ESC byte
    (?:
        \[              # CSI introducer
        [0-?]*          # parameter bytes 0x30-0x3F
        [ -/]*          # intermediate bytes 0x20-0x2F
        [@-~]           # final byte 0x40-0x7E
      |
        \]              # OSC introducer
        [^\x07\x1b]*    # any chars except BEL or ESC
        (?:\x07|\x1b\\) # terminator BEL or ESC backslash
      |
        [@-_]           # other 7-bit ESC sequences such as ESC = ESC > ESC c
    )
    """,
    re.VERBOSE,
)


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI/VT100 escape sequences from a captured-output string.

    Fast-paths the common case (no ESC byte present) so the regex pass cost
    is negligible — most tool output contains no escape byte at all.

    Args:
        text: text potentially containing CSI/OSC escape sequences.

    Returns:
        The text with all detected escape sequences removed. Non-string
        inputs are passed through unchanged so callers can stay defensive.
    """
    if not isinstance(text, str):
        return text
    if "\x1b" not in text:
        # Fast path: no escape byte present, skip the regex entirely.
        return text
    return _ANSI_ESCAPE_RE.sub("", text)
