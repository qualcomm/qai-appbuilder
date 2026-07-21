# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP middleware modules (PR-040+).

This package houses cross-cutting middleware that wraps the FastAPI app
before routers are mounted. Current inhabitants:

- :class:`CsrfMiddleware` — double-submit cookie protection over the
  ``qai_csrf`` cookie + ``X-QAI-CSRF`` header pair (PR-040).
- :class:`RequestContextMiddleware` — per-request ID + structlog context
  propagation.
- :class:`AuthMiddleware` (in :mod:`interfaces.http.middleware.auth`) —
  Qualcomm Okta OIDC + PKCE login gate. Gated behind
  ``settings.auth.enabled`` (default False); when disabled the middleware
  short-circuits so an unconfigured deployment is unaffected.

  ``AuthMiddleware`` is intentionally NOT re-exported here — the mount
  in ``apps/api/main.py`` imports it directly from
  ``interfaces.http.middleware.auth`` so the login helpers
  (``dump_session``, ``get_current_user``, ``session_secret``, …) that
  the ``/auth/*`` routes need can live alongside the middleware class
  without cluttering this package's public surface.

Per ``.importlinter`` contract ``interfaces-stays-thin``, modules in
``interfaces/http/`` may only depend on ``application/ports``,
``application/use_cases``, and ``qai.platform`` — they MUST NOT import
any ``adapters`` or ``infrastructure`` modules from a bounded context.
"""

from __future__ import annotations

from .csrf import CsrfMiddleware
from .request_context import REQUEST_ID_HEADER, RequestContextMiddleware

__all__ = ["CsrfMiddleware", "REQUEST_ID_HEADER", "RequestContextMiddleware"]
