# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Locale normalisation + i18n resolution helpers for built-in templates.

This is the SINGLE source of truth for the two locale-related primitives used
across the chat bounded context's multi-language template feature (migration
056):

* :func:`normalize_ui_language` — coerce any UI locale string to one of the
  three supported product locales (``"en"`` / ``"zh-CN"`` / ``"zh-TW"``), with
  an unknown / empty / ``None`` value falling back to ``"zh-CN"`` (the product's
  default locale). This is the canonical implementation; the adapters-layer
  ``system_prompt_builder._normalize_ui_language`` delegates here so the two
  never drift (细则 2 复用 > 重造).

* :func:`resolve_i18n` — given a per-locale JSON map (as loaded from a
  ``*_i18n_json`` column) plus the requested locale and a fallback string,
  return the localised text when present and non-empty, otherwise the fallback
  (the original single-language column).

Placed in the DOMAIN layer (not adapters) because both the repositories
(adapters) AND the discussion orchestrator (application) need it: domain is the
only layer both may depend on without a cross-layer import (application →
domain and adapters → domain are both legal; the reverse is not). The functions
are pure (no IO, no platform side-effects), matching the domain purity rule.

Forward-compatibility (AGENTS.md §8): ``resolve_i18n`` treats a missing / NULL
/ malformed i18n map as "no translation available" and returns the fallback, so
old rows (with NULL ``*_i18n_json``) and custom (``is_builtin=0``) rows always
render their original single-language text — never crashing, never blanking.
"""

from __future__ import annotations

#: The three UI locales the product supports. ``zh-CN`` is the default/fallback.
SUPPORTED_UI_LANGUAGES: tuple[str, ...] = ("en", "zh-CN", "zh-TW")

#: Default locale used when the incoming locale is unknown / empty / ``None``.
DEFAULT_UI_LANGUAGE: str = "zh-CN"


def normalize_ui_language(locale: str | None) -> str:
    """Normalize a UI locale to one of ``"en"`` / ``"zh-CN"`` / ``"zh-TW"``.

    The frontend sends ``"en"`` / ``"zh-CN"`` / ``"zh-TW"`` (the three supported
    UI locales). Anything unknown / empty / ``None`` falls back to ``"zh-CN"``
    (the product's default locale), matching the translate-prompt path's
    default-to-Simplified behaviour so the two language paths stay consistent.
    """
    lang = (locale or "").strip()
    if lang == "en":
        return "en"
    if lang == "zh-TW":
        return "zh-TW"
    return DEFAULT_UI_LANGUAGE


def resolve_i18n(
    i18n_map: dict[str, object] | None,
    locale: str,
    fallback: str,
) -> str:
    """Resolve a localised string from an i18n map with graceful fallback.

    ``i18n_map`` is a per-locale map (e.g. ``{"en": "...", "zh-CN": "...",
    "zh-TW": "..."}``) as parsed from a ``*_i18n_json`` column, or ``None`` when
    the column was NULL / the row is custom.

    Resolution rule:

    * When ``i18n_map`` carries a NON-EMPTY string value for ``locale`` (or its
      normalised form), return that translation.
    * Otherwise return ``fallback`` (the original single-language column).

    IMPORTANT — empty-string is a VALID fallback, not a missing translation: the
    caller passes ``fallback`` as the canonical value (e.g. the built-in 讨论
    mode's framing is an empty string in all three languages on purpose). This
    function only substitutes a translation when the map actually has non-empty
    text for the locale; an absent / empty translation yields ``fallback``
    unchanged (which itself may legitimately be an empty string).

    Never raises: a non-dict ``i18n_map`` or a non-string value degrades to the
    fallback (forward-compatibility — a malformed / legacy blob cannot break a
    read).
    """
    if not isinstance(i18n_map, dict):
        return fallback
    # Try the locale as given first, then its normalised form, so callers may
    # pass either a raw or an already-normalised locale.
    for key in (locale, normalize_ui_language(locale)):
        value = i18n_map.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


__all__ = [
    "SUPPORTED_UI_LANGUAGES",
    "DEFAULT_UI_LANGUAGE",
    "normalize_ui_language",
    "resolve_i18n",
]
