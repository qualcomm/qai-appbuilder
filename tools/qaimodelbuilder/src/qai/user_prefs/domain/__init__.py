# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for ``qai.user_prefs``.

PR-601a: the BC stores opaque JSON documents keyed by namespace, so the
domain reduces to two concerns:

* :class:`PrefsDocument` — a typed alias around ``dict[str, Any]`` plus
  validation invariants (no ``None`` keys, no nested ``None`` mappings
  that would erase persisted state by accident).
* :class:`PrefsKey` — a sealed string-based value object enforcing the
  ``<namespace>.<sub_key>`` layout the migration 007 ``CHECK`` constraint
  expects (length 1..128).

Both are pure Python with zero framework dependencies (domain-purity
contract per the standing import-linter forbidden set).
"""
from __future__ import annotations

import re
from typing import Any, Final, Iterable

__all__ = [
    "MAX_KEY_LENGTH",
    "PrefsDocument",
    "PrefsKey",
    "PrefsKeyError",
    "CodePersonaManager",
    "DEFAULT_PERSONA_ID",
    "DEFAULT_PERSONAS",
    "MAX_PROMPT_LENGTH",
]

#: Mirrors the ``CHECK (length(key) BETWEEN 1 AND 128)`` invariant from
#: migration 007 so we surface a domain error instead of letting SQLite
#: raise ``IntegrityError`` at the adapter boundary.
MAX_KEY_LENGTH: Final[int] = 128

#: Allowed character set for a key segment: ascii letters, digits,
#: ``_`` and ``-``.  The dot ``.`` is reserved as the namespace
#: separator and validated separately.  The pattern is intentionally
#: restrictive — every legacy ``forge_config_manager`` key fits.
_SEGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-]+$")


class PrefsKeyError(ValueError):
    """Raised when a candidate :class:`PrefsKey` violates an invariant.

    Subclassing :class:`ValueError` keeps it usable from validation
    code that catches ``ValueError`` generically while still letting
    callers distinguish user_prefs key errors via ``isinstance``.
    """


class PrefsKey:
    """A sealed ``<namespace>.<sub_key>...`` key for ``kv_user_prefs``.

    Constructed via :meth:`from_string`; the constructor is private to
    keep all validation in one place.  Two ``PrefsKey`` instances are
    equal iff their string forms are equal — they compare and hash by
    value so they can be used as dict keys in tests.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str, *, _trusted: bool = False) -> None:
        if not _trusted:
            # All public construction must go through ``from_string`` so
            # validation is not duplicated; the ``_trusted`` flag is
            # purely for the validated factory.
            raise PrefsKeyError(
                "PrefsKey must be constructed via PrefsKey.from_string()"
            )
        self._value = value

    @classmethod
    def from_string(cls, raw: str) -> "PrefsKey":
        """Validate ``raw`` and return a :class:`PrefsKey` instance.

        Raises :class:`PrefsKeyError` when:
        * the value is not a ``str``;
        * length is outside ``[1, MAX_KEY_LENGTH]``;
        * any segment fails :data:`_SEGMENT_PATTERN`;
        * leading / trailing dot or empty segment is present.
        """
        if not isinstance(raw, str):
            raise PrefsKeyError(
                f"PrefsKey must be a str, got {type(raw).__name__}"
            )
        if not (1 <= len(raw) <= MAX_KEY_LENGTH):
            raise PrefsKeyError(
                "PrefsKey length must be between 1 and "
                f"{MAX_KEY_LENGTH}, got {len(raw)}"
            )
        segments = raw.split(".")
        for seg in segments:
            if not seg:
                raise PrefsKeyError(
                    f"PrefsKey segment empty in {raw!r}"
                )
            if not _SEGMENT_PATTERN.match(seg):
                raise PrefsKeyError(
                    f"PrefsKey segment {seg!r} contains forbidden "
                    "characters; allowed [A-Za-z0-9_-]"
                )
        return cls(raw, _trusted=True)

    @property
    def value(self) -> str:
        """The validated ``str`` form, suitable for SQL parameters."""
        return self._value

    @property
    def namespace(self) -> str:
        """The first segment of the key (e.g. ``forge`` for ``forge.config``)."""
        return self._value.split(".", 1)[0]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"PrefsKey({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PrefsKey):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash((PrefsKey, self._value))


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
#: The persisted shape is an arbitrary JSON object.  We model it as a
#: ``dict[str, Any]`` because:
#:
#: * the legacy on-disk ``forge_config.json`` was already an arbitrary
#:   nested object whose schema differed across keys (per-feature
#:   blob) — promoting it to a strict typed model would break parity;
#: * downstream consumers (route layer + UI) already treat it as
#:   opaque JSON.
#:
#: We provide :func:`coerce_document` so adapters can normalise raw
#: JSON loads into a guaranteed-``dict`` shape without each call site
#: re-implementing the check.
PrefsDocument = dict[str, Any]


def coerce_document(raw: Any) -> PrefsDocument:
    """Return ``raw`` if it is a ``dict``; ``{}`` otherwise.

    Mirrors the defensive normalisation in
    :class:`qai.ai_coding.adapters.coding_config_repository.KvCodingConfigRepository.load`
    so legacy / hand-edited rows that contain a non-object payload
    surface to callers as "no document" rather than raising.
    """
    if isinstance(raw, dict):
        return raw
    return {}


from qai.user_prefs.domain.code_personas import (  # noqa: E402
    CodePersonaManager,
    DEFAULT_PERSONA_ID,
    DEFAULT_PERSONAS,
    MAX_PROMPT_LENGTH,
)


def shallow_merge(
    base: PrefsDocument,
    updates: PrefsDocument,
    *,
    _allowed_top_level: Iterable[str] | None = None,
) -> PrefsDocument:
    """Return a new dict where top-level keys in ``updates`` overwrite ``base``.

    Mirrors the legacy ``forge_config_manager.update`` semantics: only
    the top level is replaced, nested objects are taken verbatim from
    ``updates`` (no deep merge).  This keeps the contract simple and
    matches the existing UI which always sends complete sub-documents
    for whichever section it is editing.

    The optional ``_allowed_top_level`` argument lets routes constrain
    which top-level keys a request can touch (e.g. the
    ``/api/preferences`` route only allows ``ui``); unknown keys are
    silently dropped at the use case boundary, never persisted.
    """
    if _allowed_top_level is None:
        merged = dict(base)
        for k, v in updates.items():
            merged[k] = v
        return merged
    allow = frozenset(_allowed_top_level)
    merged = dict(base)
    for k, v in updates.items():
        if k in allow:
            merged[k] = v
    return merged
