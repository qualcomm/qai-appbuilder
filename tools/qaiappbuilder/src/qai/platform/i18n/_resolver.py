from __future__ import annotations

import logging
from typing import Any

from ._context import get_current_locale
from ._locale import normalize_ui_language
from .catalogs import CATALOGS

_logger = logging.getLogger(__name__)


def t(key: str, /, *, locale: str | None = None, **params: Any) -> str:
    """Resolve a catalog key to a localised, interpolated string.

    Priority:
      1. explicit ``locale=`` kwarg
      2. current-request ContextVar
      3. DEFAULT_UI_LANGUAGE ("zh-CN")

    Missing-key fallback: return ``key`` verbatim, logging a DEBUG record
    so a missing catalog entry is at least visible to developers running
    with verbose logging.

    Interpolation: Python str.format with ``**params``. A missing / extra
    placeholder catches the resulting KeyError / IndexError so the request
    never fails — but a WARNING is logged so the mismatch surfaces in the
    logs. The raw template (still containing ``{name}`` placeholders) is
    returned as the best-available fallback.
    """
    resolved = normalize_ui_language(
        locale if locale is not None else get_current_locale()
    )
    catalog = CATALOGS.get(resolved) or {}
    template = catalog.get(key)
    if template is None:
        # Try fallback to zh-CN if the requested locale is missing the key.
        template = CATALOGS["zh-CN"].get(key)
    if template is None:
        _logger.debug(
            "qai.platform.i18n: missing catalog key %r (locale=%s)",
            key,
            resolved,
        )
        return key  # ultimate fallback
    # Always attempt the format pass so a template that carries placeholders
    # like ``{name}`` STILL triggers the interpolation-failed warning below
    # when a caller forgot to pass ``name=…``. A no-op ``.format()`` on a
    # placeholder-free template returns the same string, so this is a zero-
    # cost check for the common case.
    try:
        return template.format(**params)
    except (KeyError, IndexError) as exc:
        _logger.warning(
            "qai.platform.i18n: interpolation failed for key=%r locale=%s "
            "(missing param %s); returning raw template",
            key,
            resolved,
            exc,
        )
        return template
