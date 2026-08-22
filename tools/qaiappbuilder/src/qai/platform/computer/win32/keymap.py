# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Key-name → Windows virtual-key-code (VK) mapping.

Pure data + resolution logic (no ctypes, no Win32 calls) so it is safe to
unit-test on any platform and to import from the validation layer. The
table is 1:1 with book 01 §1.3 / book 04 §5.

Resolution rules (book 04 §5):

* Matching is case-insensitive (upper-cased).
* A named alias resolves to a fixed VK.
* A single character resolves to a VK derived from its upper-case code
  point for ASCII letters/digits (``A``-``Z`` -> ``0x41``.., ``0``-``9``
  -> ``0x30``..); other single characters have no reliable VK and are
  reported as needing Unicode injection instead.
* Letters are NOT implicitly Shift-modified: ``"A"`` and ``"a"`` both
  produce the lower-case ``a`` key (VK ``0x41``) with no Shift.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "KEY_ALIASES",
    "ResolvedKey",
    "resolve_key",
]


# Named-alias → VK. Function keys F1..F24 are generated below.
KEY_ALIASES: dict[str, int] = {
    # Modifiers
    "CTRL": 0x11,
    "CONTROL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "OPTION": 0x12,
    "META": 0x5B,
    "CMD": 0x5B,
    "COMMAND": 0x5B,
    "SUPER": 0x5B,
    "WINDOWS": 0x5B,
    # Editing / whitespace
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "SPACE": 0x20,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "INSERT": 0x2D,
    # Navigation
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "UP": 0x26,
    "ARROWUP": 0x26,
    "DOWN": 0x28,
    "ARROWDOWN": 0x28,
    "LEFT": 0x25,
    "ARROWLEFT": 0x25,
    "RIGHT": 0x27,
    "ARROWRIGHT": 0x27,
    # Locks / misc
    "CAPSLOCK": 0x14,
    "NUMLOCK": 0x90,
    "PRINTSCREEN": 0x2C,
    "PRINTSCR": 0x2C,
}

# F1..F24 -> 0x70..0x87.
for _i in range(1, 25):
    KEY_ALIASES[f"F{_i}"] = 0x70 + (_i - 1)
del _i


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    """The result of resolving one key name.

    Exactly one of ``vk`` / ``char`` is set:

    * ``vk`` — a Windows virtual-key code to press/release.
    * ``char`` — the single character to inject as Unicode (used when a
      symbol has no reliable VK); ``keypress`` falls back to this path so
      no functionality is lost (book 04 §5 note).
    """

    vk: int | None = None
    char: str | None = None

    @property
    def is_unicode(self) -> bool:
        return self.vk is None and self.char is not None


def resolve_key(name: str) -> ResolvedKey:
    """Resolve one key name to a :class:`ResolvedKey`.

    Args:
        name: A key alias (case-insensitive) or a single character.

    Raises:
        ValueError: if ``name`` is empty or an unknown multi-character
            token that is not a recognised alias.
    """
    if not name:
        raise ValueError("empty key name")
    upper = name.upper()
    vk = KEY_ALIASES.get(upper)
    if vk is not None:
        return ResolvedKey(vk=vk)
    if len(name) == 1:
        ch = name
        code = ord(ch.upper())
        # ASCII letters/digits map to their VK directly.
        if 0x41 <= code <= 0x5A or 0x30 <= code <= 0x39:
            return ResolvedKey(vk=code)
        # Other single characters: inject as Unicode (no reliable VK).
        return ResolvedKey(char=ch)
    raise ValueError(f"unknown key name: {name!r}")
