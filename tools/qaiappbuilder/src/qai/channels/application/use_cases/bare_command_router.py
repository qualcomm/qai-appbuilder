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
    HELP = "help"                        # /help
    CC_HELP = "cc_help"                  # /cc help
    OC_HELP = "oc_help"                  # /oc help
    NEW = "new"                          # /new or /clear
    COMPACT = "compact"                  # /compact — force-compress now
    COMPACT_STATUS = "compact_status"    # /compact status — query only
    COMPACT_MIGRATE = "compact_migrate"  # /compact migrate — P5 handoff
    COMPACT_CLEAR = "compact_clear"      # /compact clear — drop checkpoint
    COMPACT_INVALID = "compact_invalid"  # /compact with a bad subcommand
    MODEL_SET = "model_set"              # /model <id>
    MODEL_LIST = "model_list"            # /models
    GRANT = "grant"                      # /grant ...
    REBOOT = "reboot"                    # /reboot


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


def classify_bare_command(*, verb: str, args: tuple[str, ...]) -> BareCommand | None:
    """Classify a bare slash command without performing any I/O.

    Returns a :class:`BareCommand` when ``verb`` is a bare command the
    dispatch bridge handles directly (R-11 table), or ``None`` when the
    verb falls through to ai_coding (``cc`` / ``oc`` / ``stop``) or
    conversation-command routing (``list`` / ``use`` / ``status`` /
    ``rename`` / ``delete``).

    The caller passes ``verb`` already lower-cased.  ``/compact`` sub-command
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
        # New semantics:
        #   * ``/compact``          → force-compress the current conversation
        #     immediately and return before/after occupancy;
        #   * ``/compact status``   → read-only occupancy + digest / ledger
        #     summary + escalation hint;
        #   * ``/compact migrate``  → P5 handoff (mint a fresh conversation
        #     seeded from the source's digest); errors when no digest exists;
        #   * ``/compact clear``    → drop the compaction checkpoint (digest,
        #     ledger, mid-turn counter) and start clean;
        #   * anything else → invalid.
        if not args:
            return BareCommand(kind=BareCommandKind.COMPACT)
        sub = args[0].lower()
        if sub == "status":
            return BareCommand(kind=BareCommandKind.COMPACT_STATUS)
        if sub == "migrate":
            return BareCommand(kind=BareCommandKind.COMPACT_MIGRATE)
        if sub == "clear":
            return BareCommand(kind=BareCommandKind.COMPACT_CLEAR)
        return BareCommand(kind=BareCommandKind.COMPACT_INVALID)

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
