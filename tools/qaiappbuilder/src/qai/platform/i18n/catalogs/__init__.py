# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from . import en, zh_CN, zh_TW

CATALOGS: dict[str, dict[str, str]] = {
    "en": en.MESSAGES,
    "zh-CN": zh_CN.MESSAGES,
    "zh-TW": zh_TW.MESSAGES,
}
