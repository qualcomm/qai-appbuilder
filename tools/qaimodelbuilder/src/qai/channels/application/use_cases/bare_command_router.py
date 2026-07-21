# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure classification + reply formatting for bare slash commands.

Architecture cleanup (A-1 step1): the apps-layer dispatch bridge
(:mod:`apps.api._channel_dispatch_bridge`) previously inlined both the
verb→category decision *and* the I/O orchestration for every bare slash
command inside one ``_try_bare_command`` body.  The **decision** is pure
domain logic (which verb is a bare command, how its arguments parse) and
belongs in the channels application layer; the **I/O orchestration**
(calling chat / model-catalog / grant / reboot collaborators) stays in
the bridge because it crosses contexts the channels layer may not import.

This module owns the pure half:

* :func:`classify_bare_command` — maps a parsed verb + args to a
  :class:`BareCommand` describing the category and any pre-parsed
  parameters, or ``None`` when the verb is not a bare command (the
  bridge then falls through to ai_coding / chat routing).
* :func:`format_reply_text` — the small text-shaping helpers that build
  the final user-facing reply strings (no collaborator calls).

No import of ``apps`` / ``qai.chat`` / ``qai.ai_coding`` — this is
import-linter-safe channels application code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


#: Bare slash-command categories.  Plain string constants (no Enum) so the
#: apps layer's switch table stays free of a new public Enum surface.
class BareCommandKind:
    HELP = "help"            # /help — main help text
    CC_HELP = "cc_help"      # /cc help — CC help text
    OC_HELP = "oc_help"      # /oc help — OC help text
    NEW = "new"              # /new or /clear — reset conversation
    COMPACT = "compact"      # /compact <N> (N ≥ 1) — set override + trim
    COMPACT_SHOW = "compact_show"  # /compact (no arg) — view current setting only
    COMPACT_RESET = "compact_reset"  # /compact 0 — restore global budget
    COMPACT_INVALID = "compact_invalid"  # /compact with a bad argument
    MODEL_SET = "model_set"  # /model <id>
    MODEL_LIST = "model_list"  # /models
    GRANT = "grant"          # /grant ...
    REBOOT = "reboot"        # /reboot


@dataclass(frozen=True, slots=True)
class BareCommand:
    """A classified bare slash command + its pre-parsed parameters.

    ``kind`` is one of the :class:`BareCommandKind` constants.  The
    remaining fields carry whatever the bridge needs to perform the I/O
    for that category; unused fields default to empty so the bridge can
    read them unconditionally.
    """

    kind: str
    args: tuple[str, ...] = field(default_factory=tuple)
    #: Parsed ``/compact`` round count (valid only for ``COMPACT``).
    rounds: int = 0


def classify_bare_command(*, verb: str, args: tuple[str, ...]) -> BareCommand | None:
    """Classify a bare slash command without performing any I/O.

    Returns a :class:`BareCommand` when ``verb`` is a bare command the
    dispatch bridge handles directly (R-11 table), or ``None`` when the
    verb falls through to ai_coding (``cc`` / ``oc`` / ``stop``) or
    conversation-command routing (``list`` / ``use`` / ``status`` /
    ``rename`` / ``delete``).

    The caller passes ``verb`` already lower-cased.  ``/compact`` argument
    parsing is done here (pure) so the bridge only has to branch on the
    resulting kind.
    """
    if verb == "help":
        return BareCommand(kind=BareCommandKind.HELP)

    if verb == "cc" and args and args[0].lower() == "help":
        return BareCommand(kind=BareCommandKind.CC_HELP)
    if verb == "oc" and args and args[0].lower() == "help":
        return BareCommand(kind=BareCommandKind.OC_HELP)

    if verb in ("new", "clear"):
        return BareCommand(kind=BareCommandKind.NEW)

    if verb == "compact":
        # 4-M8 — V1 ``wechat/channel.py:1706-1739``:
        #   * bare ``/compact`` (no arg) → **view-only**: echo the current
        #     per-user / global setting WITHOUT trimming or setting an
        #     override (V1 lines 1708-1716).  Previously V2 defaulted the
        #     no-arg form to ``rounds=5`` which silently trimmed to 5 and set
        #     an override — a regression vs V1.  Now classified as
        #     ``COMPACT_SHOW`` so the bridge only reports the current state.
        #   * ``/compact 0`` restores the global budget (clears the per-user
        #     override);
        #   * ``/compact N`` (N ≥ 1) sets the per-user rounds + trims;
        #   * a non-integer / negative argument is invalid.
        if not args:
            return BareCommand(kind=BareCommandKind.COMPACT_SHOW)
        try:
            rounds = int(args[0])
        except ValueError:
            return BareCommand(kind=BareCommandKind.COMPACT_INVALID)
        if rounds == 0:
            return BareCommand(kind=BareCommandKind.COMPACT_RESET, rounds=0)
        if rounds < 0:
            return BareCommand(kind=BareCommandKind.COMPACT_INVALID)
        return BareCommand(kind=BareCommandKind.COMPACT, rounds=rounds)

    if verb == "model":
        return BareCommand(kind=BareCommandKind.MODEL_SET, args=args)
    if verb == "models":
        return BareCommand(kind=BareCommandKind.MODEL_LIST)

    if verb == "grant":
        return BareCommand(kind=BareCommandKind.GRANT, args=args)

    if verb == "reboot":
        return BareCommand(kind=BareCommandKind.REBOOT)

    return None


# ---------------------------------------------------------------------------
# Reply text shaping (pure)
# ---------------------------------------------------------------------------
def format_model_list_reply(model_ids: list[str]) -> str:
    """Format the ``/models`` reply from a flat list of model ids.

    Returns the "no models" notice when ``model_ids`` is empty, otherwise
    a bulleted list under a header.  Pure string shaping — the bridge
    resolves the ids from the catalog use case and hands them here.
    """
    if not model_ids:
        return "\u2139\ufe0f 当前没有可用模型。"
    lines = ["\U0001f4cb 可用模型："] + [f"  • {m}" for m in model_ids]
    return "\n".join(lines)
