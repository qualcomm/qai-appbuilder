# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Canonical QAIRT/AppBuilder version parsing, ordering, and wheel policy.

Two immutable version types:

* :class:`QairtVersion` — the installed SDK release, ``A.B.C`` or the
  four-segment distribution form ``A.B.C.D`` (the trailing segment is a build
  stamp, e.g. ``2.49.40.260810``).
* :class:`AppBuilderVersion` — the ``qai_appbuilder`` extension release,
  always exactly three segments ``A.B.C``.

Both parsers are strict: no prefixes, suffixes, surrounding whitespace, sign
characters, or non-numeric segments. All comparisons are semantic (numeric
per component), never lexicographic string comparisons.

For SDK ``A.B.C[.D]`` the minimum compatible extension is ``A.B.C`` (Locked
Contract 8). If installation is required, exactly four AppBuilder candidates
are tried: ``A.B.C``, ``A.(B+1).0``, ``A.(B+2).0``, ``A.(B+3).0`` (Locked
Contract 9).

Wheel assets are named deterministically for the current CPython 3.13 /
Windows architecture only (Locked Contract 10); any other Python ABI,
platform, or architecture is rejected before any network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_QAIRT_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")
_APPBUILDER_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: The only Python ABI/platform combinations the product ever downloads for.
_SUPPORTED_PYTHON_TAGS: frozenset[str] = frozenset({"cp313-cp313"})
_SUPPORTED_PLATFORM_TAGS: frozenset[str] = frozenset({"win_arm64", "win_amd64"})


class UnsupportedPlatformError(RuntimeError):
    """Raised when a wheel is requested for an unsupported OS/ABI/architecture."""


@dataclass(frozen=True, slots=True)
class AppBuilderVersion:
    """An immutable, strictly three-segment ``qai_appbuilder`` release version."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for name in ("major", "minor", "patch"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"AppBuilderVersion.{name} must be a non-negative int, got {value!r}"
                )

    @staticmethod
    def parse(raw: str) -> AppBuilderVersion:
        """Strictly parse ``A.B.C``; raise :class:`ValueError` on anything else."""

        match = _APPBUILDER_VERSION_RE.fullmatch(raw)
        if match is None:
            raise ValueError(f"Not a strict three-segment AppBuilder version: {raw!r}")
        major, minor, patch = (int(part) for part in match.groups())
        return AppBuilderVersion(major=major, minor=minor, patch=patch)

    def _sort_key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __lt__(self, other: AppBuilderVersion) -> bool:
        if not isinstance(other, AppBuilderVersion):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def __le__(self, other: AppBuilderVersion) -> bool:
        if not isinstance(other, AppBuilderVersion):
            return NotImplemented
        return self._sort_key() <= other._sort_key()

    def __gt__(self, other: AppBuilderVersion) -> bool:
        if not isinstance(other, AppBuilderVersion):
            return NotImplemented
        return self._sort_key() > other._sort_key()

    def __ge__(self, other: AppBuilderVersion) -> bool:
        if not isinstance(other, AppBuilderVersion):
            return NotImplemented
        return self._sort_key() >= other._sort_key()

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class QairtVersion:
    """An immutable QAIRT SDK release version: ``A.B.C`` or ``A.B.C.D``.

    ``build`` is the optional fourth distribution segment (for example the
    ``260810`` in ``2.49.40.260810``); ``None`` when the SDK directory name
    has only three segments.
    """

    major: int
    minor: int
    patch: int
    build: int | None = None

    def __post_init__(self) -> None:
        for name in ("major", "minor", "patch"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"QairtVersion.{name} must be a non-negative int, got {value!r}")
        if self.build is not None and (
            not isinstance(self.build, int) or isinstance(self.build, bool) or self.build < 0
        ):
            raise ValueError(
                f"QairtVersion.build must be None or a non-negative int, got {self.build!r}"
            )

    @staticmethod
    def parse(raw: str) -> QairtVersion:
        """Strictly parse ``A.B.C`` or ``A.B.C.D``; raise :class:`ValueError` otherwise."""

        match = _QAIRT_VERSION_RE.fullmatch(raw)
        if match is None:
            raise ValueError(f"Not a strict three- or four-segment QAIRT version: {raw!r}")
        major_s, minor_s, patch_s, build_s = match.groups()
        build = int(build_s) if build_s is not None else None
        return QairtVersion(major=int(major_s), minor=int(minor_s), patch=int(patch_s), build=build)

    @property
    def minimum_appbuilder(self) -> AppBuilderVersion:
        """The minimum compatible extension version ``A.B.C`` (Locked Contract 8)."""

        return AppBuilderVersion(major=self.major, minor=self.minor, patch=self.patch)

    def appbuilder_candidates(
        self,
    ) -> tuple[AppBuilderVersion, AppBuilderVersion, AppBuilderVersion, AppBuilderVersion]:
        """Return ``A.B.C`` plus the next three minor ``.0`` releases.

        Per Locked Contract 9, this is the exhaustive, exactly-four candidate
        list ever attempted — never an unbounded "latest" search.
        """

        minimum = self.minimum_appbuilder
        return (
            minimum,
            AppBuilderVersion(major=self.major, minor=self.minor + 1, patch=0),
            AppBuilderVersion(major=self.major, minor=self.minor + 2, patch=0),
            AppBuilderVersion(major=self.major, minor=self.minor + 3, patch=0),
        )

    def _sort_key(self) -> tuple[int, int, int, int]:
        return (self.major, self.minor, self.patch, self.build if self.build is not None else 0)

    def __lt__(self, other: QairtVersion) -> bool:
        if not isinstance(other, QairtVersion):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def __le__(self, other: QairtVersion) -> bool:
        if not isinstance(other, QairtVersion):
            return NotImplemented
        return self._sort_key() <= other._sort_key()

    def __gt__(self, other: QairtVersion) -> bool:
        if not isinstance(other, QairtVersion):
            return NotImplemented
        return self._sort_key() > other._sort_key()

    def __ge__(self, other: QairtVersion) -> bool:
        if not isinstance(other, QairtVersion):
            return NotImplemented
        return self._sort_key() >= other._sort_key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.build is None else f"{base}.{self.build}"


def appbuilder_wheel_asset_name(
    version: AppBuilderVersion, *, python_tag: str, platform_tag: str
) -> str:
    """Return the deterministic exact wheel asset name for ``version``.

    Only the current CPython 3.13 / Windows arm64/amd64 combination is
    supported; every other ``python_tag``/``platform_tag`` is rejected with
    :class:`UnsupportedPlatformError` before any network access (Locked
    Contract 10). The name never carries an undocumented distribution suffix:
    ``qai_appbuilder-{version}-{python_tag}-{platform_tag}.whl``.
    """

    if python_tag not in _SUPPORTED_PYTHON_TAGS or platform_tag not in _SUPPORTED_PLATFORM_TAGS:
        raise UnsupportedPlatformError(
            f"Unsupported wheel target: python_tag={python_tag!r}, platform_tag={platform_tag!r}"
        )
    return f"qai_appbuilder-{version}-{python_tag}-{platform_tag}.whl"


__all__ = [
    "AppBuilderVersion",
    "QairtVersion",
    "UnsupportedPlatformError",
    "appbuilder_wheel_asset_name",
]
