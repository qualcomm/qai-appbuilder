"""Per-request current-locale ContextVar.

Set by the HTTP middleware (:mod:`interfaces.http.middleware.locale`) at
the entry of every request. :func:`qai.platform.i18n.t` reads this if no
explicit ``locale=`` kwarg is given.
"""
from __future__ import annotations
from contextvars import ContextVar
from ._locale import DEFAULT_UI_LANGUAGE, normalize_ui_language

_CURRENT_LOCALE: ContextVar[str] = ContextVar(
    "qai_current_locale", default=DEFAULT_UI_LANGUAGE
)


def get_current_locale() -> str:
    """Return the locale bound to the current async context.

    Falls back to :data:`~qai.platform.i18n.DEFAULT_UI_LANGUAGE` when no
    request has set one (e.g. CLI startup, background tasks, or a code
    path that runs before :class:`LocaleMiddleware`).
    """
    return _CURRENT_LOCALE.get()


def set_current_locale(locale: str | None) -> str:
    """Pin ``locale`` (or the default) to the current async context.

    ``locale=None`` — or any non-supported value — normalises to
    :data:`~qai.platform.i18n.DEFAULT_UI_LANGUAGE` (``"zh-CN"``), so
    callers that lack request context (background tasks, tests) can
    reset the ContextVar to a known default by calling
    ``set_current_locale(None)``.

    Returns the normalised locale that was actually stored, so callers
    can log / react to whichever value the resolver will now see.
    """
    resolved = normalize_ui_language(locale)
    _CURRENT_LOCALE.set(resolved)
    return resolved
