# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`CommandParserPort` adapter — RegexCommandParser (PR-203).

Parses leading-slash commands (``/<verb> <arg1> <arg2> ...``) out of a
:class:`MessageContent` body and returns a structured :class:`Command`.
Returns ``None`` when the message is plain chat (no leading ``/``), so
the dispatcher routes it to the LLM bridge rather than a verb handler.

Recognised canonical verbs (PR-203 — the 9 in scope):

* ``cc`` — Claude-Code session controls.  First arg may be a
  subcommand (``new`` / ``list`` / ``use`` / ``rename`` / ``delete`` /
  ``status`` / ``effort``); subcommands are lower-cased.
* ``oc`` — OpenCode session controls; same subcommand shape as ``/cc``.
* ``grant`` — sandbox grant.  Arg is a path or ``"all"`` / ``"list"``.
* ``list`` — list current channel sessions (no args).
* ``use`` — switch active session: ``/use <id_or_alias>``.
* ``status`` — show current channel session status (no args).
* ``rename`` — ``/rename <new_name>`` (free-form trailing text).
* ``delete`` — ``/delete <id_or_current>``.
* ``help`` — show command list (free-form trailing topic allowed).
* ``new`` — create a new session.
* ``clear`` — clear the current session context.
* ``compact`` — compact/summarize the session context.
* ``model`` — switch or display current model.
* ``models`` — list available models.
* ``stop`` — stop the current generation.
* ``reboot`` — restart the server.

Aliases (resolve to canonical verbs at parse time):

* ``/h`` and ``/?``  →  ``help``
* ``/c``             →  ``compact``  (S9 PR-093 §3.1 F-9: restored to legacy
                        semantics — ``/c 5`` means "/compact 5", *not* /cc)
* ``/code``          →  ``cc``  (S9 PR-093 §3.1 F-9: new explicit alias for
                        Claude-Code, replaces the ambiguous ``/c`` shorthand)
* ``/o``             →  ``oc``
* ``/ls`` and ``/l`` →  ``list``
* ``/n``             →  ``new``
* ``/cl``            →  ``clear``
* ``/m``             →  ``model``
* ``/ms``            →  ``models``
* ``/st``            →  ``stop``
* ``/r``             →  ``reboot``
* ``/rn``            →  ``rename``
* ``/del``           →  ``delete``
* ``/g``             →  ``grant``

Design notes
------------

* Verbs are case-insensitive when matched; the returned :class:`Command`
  carries the canonical lower-case form so downstream dispatch is
  stable.
* Aliases are resolved before subcommand canonicalisation —
  ``/c list`` becomes ``Command(verb="cc", args=("list",))``.
* For verbs whose first arg is a subcommand (``cc`` / ``oc``), the
  first arg is lower-cased; the remaining args are preserved as-is.
* For "free-form trailing text" verbs (``rename`` and ``help``) the
  rest of the line after the verb is preserved as a *single* trailing
  arg (so ``/rename my new name`` →
  ``Command(verb="rename", args=("my new name",))``).
* For other verbs args are split on whitespace.
* An unrecognised verb still produces a :class:`Command` (with
  ``args=()``) so the dispatch use case can surface "unknown command"
  feedback — this matches legacy behaviour.
* :class:`InvalidCommandError` is raised only when the input *looks*
  like a command (starts with ``/``) but is malformed (e.g. a bare
  ``/`` or whitespace-only verb slot) — these inputs are not plausibly
  plain chat.
* Verb length is capped at 64 chars to match :class:`Command`'s
  validation; over-long verbs fall back to ``None`` (treated as plain
  chat) — typing
  ``/incrediblylongnonsensicaltextthatisntactuallyacommand`` is more
  plausibly chat than malice.
"""

from __future__ import annotations

import re

from qai.channels.domain import Command, InvalidCommandError, MessageContent

__all__ = ["RegexCommandParser"]


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

# Matches a leading slash, then a verb of word-chars (≤ 64), then any
# remaining text (captured verbatim and split on whitespace below).
#
# The verb pattern accepts a leading letter + word-chars OR the bare
# ``?`` literal — the ``?`` form is a recognised alias for ``/help``
# (mirrors the legacy ``backend/channels/session_commands.py`` grammar).
_COMMAND_RE = re.compile(
    r"^/(?P<verb>\?|[A-Za-z][A-Za-z0-9_-]{0,63})(?:\s+(?P<rest>.*))?$"
)

# Canonical verbs in scope for PR-203 + C3 extensions.
_CANONICAL_VERBS: frozenset[str] = frozenset(
    {
        "cc", "oc", "grant", "list", "use", "status", "rename", "delete",
        "help", "new", "clear", "compact", "model", "models", "stop",
        "reboot",
    }
)

# Alias → canonical mapping (lower-case keys).
#
# S9 PR-093 §3.1 F-9 restored legacy semantics:
#   * ``/c`` → ``compact`` (was previously aliased to ``cc`` which was a
#     breaking change for users typing ``/c 5`` to compact context).
#   * ``/code`` → ``cc`` is the new explicit alias for Claude-Code, so
#     users still have a short form without the ambiguity of ``/c``.
_ALIASES: dict[str, str] = {
    "h": "help",
    "?": "help",
    "c": "compact",
    "code": "cc",
    "o": "oc",
    "ls": "list",
    "l": "list",
    "n": "new",
    "cl": "clear",
    "m": "model",
    "ms": "models",
    "st": "stop",
    "r": "reboot",
    "rn": "rename",
    "del": "delete",
    "g": "grant",
}

# Verbs whose first positional arg is a subcommand and should be
# canonicalised to lower-case.
_SUBCOMMAND_VERBS: frozenset[str] = frozenset({"cc", "oc"})

# 4-M1 — /cc and /oc subcommand short-name aliases.  V1 truth:
# ``backend/channels/session_commands.py:133-159``
# (``_CC_SUBCOMMAND_ALIASES`` / ``_OC_SUBCOMMAND_ALIASES``).  Only the
# officially-documented short names are mapped (no arbitrary prefix
# matching).  ``/cc l`` → ``/cc list``, ``/cc u 2`` → ``/cc use 2`` …
#
# ``cd`` / ``fork`` are CC-only verbs but the alias ``f`` → ``fork`` is
# only meaningful for ``/cc``; we keep one shared table because ``oc``
# simply never receives ``f`` in practice (the dispatch surfaces an
# "unknown subcommand" reply for ``/oc f`` either way, matching V1).
_CC_OC_SUBCOMMAND_ALIASES: dict[str, str] = {
    "ms": "models",
    "m": "model",
    "l": "list",
    "u": "use",
    "s": "status",
    "st": "stop",
    "f": "fork",
    "r": "rename",
    "c": "close",
    "d": "delete",
    "h": "help",
    "n": "new",
}

# Verbs whose remaining text is treated as a single free-form trailing
# arg rather than whitespace-split.
_FREEFORM_TRAILING_VERBS: frozenset[str] = frozenset({"rename", "help"})


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class RegexCommandParser:
    """Regex-based :class:`CommandParserPort` implementation (PR-203).

    Recognises the 16 canonical verbs + 15 aliases listed in the module
    docstring.  Unknown verbs still yield a :class:`Command` so the
    dispatch use case can render an "unknown command" reply — this
    keeps parity with the legacy ``backend/channels/session_commands.py``
    behaviour.
    """

    __slots__ = ()

    def parse(self, content: MessageContent) -> Command | None:
        text = content.text.strip()
        if not text.startswith("/"):
            return None

        # Bare "/" or whitespace-only verb slot — looks like a command
        # attempt, but unparseable.
        body = text[1:]  # strip leading slash
        if not body or body.isspace():
            raise InvalidCommandError(
                "command text contains no verb after '/'"
            )

        match = _COMMAND_RE.match(text)
        if match is None:
            # Verb either over-length or starts with a non-letter.  Per
            # docstring, fall back to None (plain chat) for over-length
            # verbs; everything else (e.g. ``/123``) we also treat as
            # plain chat to preserve the legacy lenient behaviour for
            # non-letter leads.
            return None

        raw_verb = match.group("verb").lower()
        rest = (match.group("rest") or "").strip()

        # Resolve alias → canonical.
        verb = _ALIASES.get(raw_verb, raw_verb)

        # Build args according to the verb's shape.
        args = self._build_args(verb, rest)
        return Command(verb=verb, args=args)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _build_args(verb: str, rest: str) -> tuple[str, ...]:
        """Return the args tuple for ``verb`` given the trailing ``rest``.

        * Unknown verbs → ``()`` regardless of trailing text (matches the
          legacy "surface as unknown command" UX; we don't want to leak
          arbitrary user text into the dispatch path).
        * Free-form trailing verbs (``rename`` / ``help``) → single arg
          containing the entire trailing text (after stripping).
        * Subcommand verbs (``cc`` / ``oc``) → first arg lower-cased,
          the rest preserved verbatim from whitespace-split.
        * All other canonical verbs → plain whitespace split.
        """
        if verb not in _CANONICAL_VERBS:
            # Unknown verb: drop trailing args to match the spec's
            # ``Command(verb=<unknown>, args=())`` contract.
            return ()

        if not rest:
            return ()

        if verb in _FREEFORM_TRAILING_VERBS:
            # Whole rest is one logical arg.
            return (rest,)

        parts = rest.split()
        if not parts:
            return ()

        if verb in _SUBCOMMAND_VERBS:
            head = parts[0].lower()
            # 4-M10 — ``/cc <digit>`` / ``/oc <digit>`` numeric shortcut.
            # V1 truth: ``backend/channels/wechat/channel.py:1783-1789`` /
            # ``feishu 1065-1069`` rewrite ``/cc 2`` → ``/cc use 2`` before
            # dispatch.  We inject the implicit ``use`` subcommand here so
            # the bridge's ``use`` resolver (4-M2) sees ``("use", "2")``.
            if head.isdigit():
                return ("use", head, *tuple(parts[1:]))
            # 4-M1 — expand the documented subcommand short-name alias
            # (``l`` → ``list``, ``u`` → ``use`` …) when it is not already
            # a full subcommand name.
            head = _CC_OC_SUBCOMMAND_ALIASES.get(head, head)
            tail = tuple(parts[1:])
            return (head, *tail)

        return tuple(parts)
