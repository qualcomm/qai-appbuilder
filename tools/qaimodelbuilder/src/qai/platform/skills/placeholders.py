# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.skills.placeholders — SKILL.md placeholder expansion (shared kernel).

Cross-BC utility for expanding the two placeholders a ``SKILL.md`` body may
carry so that a SKILL loaded on demand (``read`` / ``skill`` tool in the
``ai_coding`` context, or the ``skill_pack`` skill loader in the ``chat``
context) resolves paths the same way it would if it had been injected into
the system prompt by ``FeatureSkillProvider``.

Placeholders handled:

* ``${APP_ROOT}``  → the per-request install/repo root (bound via
  :func:`set_app_root`, read via :func:`get_app_root`).
* ``${SKILL_DIR}`` → the SKILL.md file's own parent directory (passed by
  the caller via the ``skill_dir`` argument).

A placeholder whose value is unavailable (no bound APP_ROOT / no
``skill_dir``) is left verbatim — never crash, never fabricate a path.

The ``APP_ROOT`` binding is a :class:`contextvars.ContextVar` so each
request / task can bind its own value without leaking to concurrent
requests. It lives in the platform shared kernel (rather than under
``qai.ai_coding``) because both the ``ai_coding`` tool handlers and the
``chat`` skill loader need to read it and Bounded-Context isolation
(``.importlinter`` contract 3) forbids ``qai.chat`` from importing
``qai.ai_coding``.
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = [
    "expand_skill_placeholders",
    "get_app_root",
    "reset_app_root",
    "set_app_root",
]


# ---------------------------------------------------------------------------
# APP_ROOT per-request binding (moved from qai.ai_coding _shared.py so that
# qai.chat can read the same ContextVar without crossing BC boundaries).
# ---------------------------------------------------------------------------
_app_root_var: ContextVar[str | None] = ContextVar(
    "qai_tool_app_root", default=None
)


def set_app_root(app_root: str | None) -> object:
    """Bind the per-request install/repo root (``${APP_ROOT}``); returns token.

    Pass the token to :func:`reset_app_root` once the request completes. A
    blank / ``None`` value clears the binding.
    """
    cleaned = (app_root or "").strip() or None
    return _app_root_var.set(cleaned)


def reset_app_root(token: object) -> None:
    """Restore the APP_ROOT binding to its previous value."""
    try:
        _app_root_var.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):  # pragma: no cover — defensive
        pass


def get_app_root() -> str | None:
    """Return the current per-request APP_ROOT, or ``None``."""
    return _app_root_var.get()


def expand_skill_placeholders(body: str, *, skill_dir: str | None = None) -> str:
    """Expand SKILL.md path placeholders in ``body`` to real absolute paths.

    Handles the two placeholders a SKILL.md may carry so that a SKILL loaded on
    demand (``read`` / ``skill`` tool) resolves the same way it would if it had
    been injected into the system prompt by ``FeatureSkillProvider``:

    * ``${APP_ROOT}``  → the per-request install/repo root (:func:`get_app_root`).
    * ``${SKILL_DIR}`` → the SKILL.md file's own parent directory (``skill_dir``).

    A placeholder whose value is unavailable (no bound APP_ROOT / no
    ``skill_dir``) is left verbatim — never crash, never fabricate a path.
    """
    if "${APP_ROOT}" in body:
        app_root = get_app_root()
        if app_root:
            body = body.replace("${APP_ROOT}", app_root)
    if skill_dir and "${SKILL_DIR}" in body:
        body = body.replace("${SKILL_DIR}", skill_dir)
    return body
