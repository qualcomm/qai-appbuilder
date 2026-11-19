# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai-appbuilder`` is resolved from PyPI on Windows ARM64 only.

* The marker must NOT cover x64: the PyPI ``win_amd64`` wheel does not load on a
  real x64 CPU (verified: "DLL initialization routine failed"); x64 takes the
  ``<version>.1`` wheel from the GitHub release.
* The lower bound is written in ``pyproject.toml`` (it cannot read JSON), so a
  test keeps it equal to the minimum extension version derived from
  ``scripts/qairt_release.json`` through ``QairtVersion.minimum_appbuilder``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

from qai.platform.qairt_switch.versioning import QairtVersion

_ROOT = Path(__file__).resolve().parents[3]

_ARM64_WIN = {"sys_platform": "win32", "platform_machine": "ARM64"}
_X64_WIN = {"sys_platform": "win32", "platform_machine": "AMD64"}
_LINUX_ARM = {"sys_platform": "linux", "platform_machine": "aarch64"}
_LINUX_X64 = {"sys_platform": "linux", "platform_machine": "x86_64"}


def _requirement() -> Requirement:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found = [
        Requirement(x)
        for x in data["project"]["dependencies"]
        if Requirement(x).name.lower().replace("_", "-") == "qai-appbuilder"
    ]
    assert len(found) == 1, "qai-appbuilder must be declared exactly once"
    return found[0]


def test_only_windows_arm64_uses_pypi() -> None:
    marker = _requirement().marker
    assert marker is not None
    assert marker.evaluate(_ARM64_WIN) is True
    assert marker.evaluate(_X64_WIN) is False  # real x64 needs the GitHub .1 wheel
    assert marker.evaluate(_LINUX_ARM) is False
    assert marker.evaluate(_LINUX_X64) is False


def test_lower_bound_matches_the_pinned_qairt_sdk() -> None:
    release = json.loads(
        (_ROOT / "scripts" / "qairt_release.json").read_text(encoding="utf-8-sig")
    )
    minimum = QairtVersion.parse(release["qairt_version"]).minimum_appbuilder
    lower = [s for s in _requirement().specifier if s.operator == ">="]
    assert len(lower) == 1
    assert Version(lower[0].version) == Version(str(minimum))


def test_has_a_major_version_ceiling() -> None:
    upper = [s for s in _requirement().specifier if s.operator == "<"]
    assert [s.version for s in upper] == ["3"]
