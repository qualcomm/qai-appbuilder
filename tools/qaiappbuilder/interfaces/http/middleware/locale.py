# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-request UI locale resolution + ContextVar propagation.

Runs EARLY in the middleware chain so downstream route handlers /
dependency-injected use cases can call ``qai.platform.i18n.t(key)`` and
automatically get the right language for the caller.

Resolution priority (first non-empty wins):

1. ``?locale=<en|zh-CN|zh-TW>`` query param (WebUI transport already
   sends this on chat SSE / persona REST calls; §2.4 of
   ``docs/30-ui-ux/i18n-implementation-plan.md``).
2. ``Accept-Language`` header — iterate the comma-separated tags in the
   order they appear (matching the browser's own preference order after
   quality-value sort) and pick the FIRST tag that normalises to one of
   :data:`~qai.platform.i18n.SUPPORTED_UI_LANGUAGES`. Falls through to
   the default when no tag matches.
3. :data:`~qai.platform.i18n.DEFAULT_UI_LANGUAGE` (``"zh-CN"``).

Non-string / malformed inputs never raise: ``normalize_ui_language``
treats them as unknown and falls back to the default.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from qai.platform.i18n import (
    SUPPORTED_UI_LANGUAGES,
    normalize_ui_language,
    set_current_locale,
)


def _pick_locale(request: Request) -> str | None:
    """Extract a UI locale from the request or ``None`` to use the default.

    Explicit ``?locale=`` query wins. Otherwise iterate every tag in
    ``Accept-Language`` and return the FIRST tag that normalises to a
    supported UI language, so a browser sending
    ``en-US,en;q=0.9,zh-CN;q=0.8`` still finds ``en`` even though its
    first tag ``en-US`` is not itself one of our three canonical codes.
    """
    q = request.query_params.get("locale")
    if q:
        return q
    header = request.headers.get("accept-language")
    if not header:
        return None
    for raw in header.split(","):
        # Drop any ``;q=`` quality suffix and surrounding whitespace.
        primary = raw.split(";", 1)[0].strip()
        if not primary:
            continue
        if normalize_ui_language(primary) == primary:
            # Exact match against SUPPORTED_UI_LANGUAGES (post-normalise).
            return primary
        # Language-only match (e.g. ``en-US`` -> ``en``) if a supported
        # locale shares the primary subtag. Strip the region and retry.
        subtag = primary.split("-", 1)[0]
        if subtag in SUPPORTED_UI_LANGUAGES:
            return subtag
    return None


class LocaleMiddleware(BaseHTTPMiddleware):
    """Extract UI locale from the request and pin it to the ContextVar."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(  # type: ignore[override]
        self, request: Request, call_next
    ) -> Response:
        # Defensive: any exception from parsing MUST NOT break the request
        # chain — fall back to the default locale instead.
        try:
            picked = _pick_locale(request)
        except Exception:  # noqa: BLE001 — locale is best-effort
            picked = None
        set_current_locale(picked)
        return await call_next(request)


__all__ = ["LocaleMiddleware"]
