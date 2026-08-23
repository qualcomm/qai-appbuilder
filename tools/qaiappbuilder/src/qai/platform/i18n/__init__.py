# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.i18n — backend i18n resolver + catalog.

Use ``t(key, **params)`` from route handlers / command bridges to look up
localised text for the CURRENT request. Middleware
(:mod:`interfaces.http.middleware.locale_middleware`) sets the request
locale at the entry of every HTTP handler; other callers may pass an
explicit ``locale=`` kwarg.

See ``docs/30-ui-ux/i18n-implementation-plan.md`` for the design.
"""
from ._context import get_current_locale, set_current_locale
from ._locale import (
    DEFAULT_UI_LANGUAGE,
    SUPPORTED_UI_LANGUAGES,
    normalize_ui_language,
)
from ._resolver import t

__all__ = [
    "DEFAULT_UI_LANGUAGE",
    "SUPPORTED_UI_LANGUAGES",
    "get_current_locale",
    "normalize_ui_language",
    "set_current_locale",
    "t",
]
