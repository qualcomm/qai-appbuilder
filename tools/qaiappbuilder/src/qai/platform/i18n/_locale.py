"""Locale normalisation for qai.platform.i18n.

Behavior-identical copy of :func:`qai.chat.domain.template_i18n.normalize_ui_language`
plus :data:`SUPPORTED_UI_LANGUAGES` + :data:`DEFAULT_UI_LANGUAGE`. Platform
sits BELOW domain in the layering, so it cannot import from ``qai.chat`` —
the two implementations are kept in lock-step manually. If you change one,
ALSO change the other.

Behavior contract shared with the domain version:

- Accepts ``str`` and ``None``.
- Whitespace-tolerant: ``"  en  "`` normalises to ``"en"``.
- Unknown / empty / non-``str`` values fall back to
  :data:`DEFAULT_UI_LANGUAGE` (``"zh-CN"``).
"""
from __future__ import annotations

SUPPORTED_UI_LANGUAGES: tuple[str, ...] = ("en", "zh-CN", "zh-TW")
DEFAULT_UI_LANGUAGE: str = "zh-CN"


def normalize_ui_language(locale: str | None) -> str:
    """Coerce ``locale`` to one of :data:`SUPPORTED_UI_LANGUAGES`.

    Mirrors the domain-layer implementation
    (:func:`qai.chat.domain.template_i18n.normalize_ui_language`) so the
    two paths never diverge. Unknown / empty / ``None`` -> ``"zh-CN"``.
    """
    lang = (locale or "").strip() if isinstance(locale, str) else ""
    if lang == "en":
        return "en"
    if lang == "zh-TW":
        return "zh-TW"
    return DEFAULT_UI_LANGUAGE
