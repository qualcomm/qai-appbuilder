# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Help-text formatters for channel commands (S9 PR-093 §2.4 L-5).

Restores the three Chinese help strings (``format_main_help`` /
``format_cc_help`` / ``format_oc_help``) that the legacy
``backend/channels/help_text.py`` provided to WeChat / Feishu /
WebUI Chat users.  Without this adapter ``/help`` / ``/cc help`` /
``/oc help`` would fall through to the unknown-command path —
addressed by parity-audit row §2.4 L-5.

Pure functions, no I/O, no globals; the channel dispatch bridge
(:mod:`apps.api._channel_dispatch_bridge`) imports
:func:`format_main_help` etc. and feeds the returned string straight
into the realtime delivery service.

Channel-specific divergence
---------------------------
The legacy code accepted a ``channel`` parameter (``"wechat"`` /
``"feishu"`` / ``"webui"``) but rendered identical text for all
three; the parameter is preserved here so callers don't need to
change but the body is the single shared text.  Future per-channel
tweaks (e.g. removing ``/reboot`` from ``webui``) can be added by
branching on ``channel`` without breaking callers.
"""

from __future__ import annotations

from qai.platform.i18n import t

__all__ = [
    "format_main_help",
    "format_cc_help",
    "format_oc_help",
]


def format_main_help(channel: str = "wechat") -> str:
    """Return the main ``/help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_main_help` from
    the legacy code path verbatim so existing channel users see no
    text regression.  ``channel`` is accepted for forward-compat but
    currently unused — every channel renders the same body.
    """
    _ = channel  # accepted for parity; future per-channel branching hook
    return t("channels.help.main")


def format_cc_help(channel: str = "wechat") -> str:
    """Return the ``/cc help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_cc_help`.
    """
    _ = channel
    return t("channels.help.cc")


def format_oc_help(channel: str = "wechat") -> str:
    """Return the ``/oc help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_oc_help`.
    """
    _ = channel
    return t("channels.help.oc")
