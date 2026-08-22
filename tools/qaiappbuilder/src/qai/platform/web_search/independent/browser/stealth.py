# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Anti-fingerprint patch for the browser search engine.

Thin re-export of the shared browser-automation stealth primitive
(:mod:`qai.platform.web_automation.stealth`). The patch itself lives in the
neutral shared-kernel module so the interactive browser tool and this search
engine apply an identical fingerprint; this module preserves the engine's
historical import path (``...independent.browser.stealth``) unchanged.
"""

from __future__ import annotations

from qai.platform.web_automation.stealth import apply_stealth, stealth_init_script

__all__ = ["apply_stealth", "stealth_init_script"]
