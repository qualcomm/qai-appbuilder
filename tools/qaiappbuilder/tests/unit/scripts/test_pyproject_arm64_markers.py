# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Windows ARM64 must not hard-require a package PyPI cannot supply there.

PyPI has no ``win_arm64`` wheel for ``tiktoken`` (the sdist needs a Rust
toolchain), so an unconditional requirement made every ARM64 install depend on
the vendor bundle. The requirement is therefore conditional; chat degrades to a
coarse token estimate when tiktoken is absent.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"

_ARM64_WIN = {"sys_platform": "win32", "platform_machine": "ARM64"}
_X64_WIN = {"sys_platform": "win32", "platform_machine": "AMD64"}
_LINUX_ARM = {"sys_platform": "linux", "platform_machine": "aarch64"}


def _core_requirements() -> dict[str, Requirement]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    reqs = [Requirement(x) for x in data["project"]["dependencies"]]
    return {r.name.lower(): r for r in reqs}


def test_tiktoken_is_not_required_on_windows_arm64() -> None:
    marker = _core_requirements()["tiktoken"].marker
    assert marker is not None
    assert marker.evaluate(_ARM64_WIN) is False


def test_tiktoken_is_still_required_elsewhere() -> None:
    marker = _core_requirements()["tiktoken"].marker
    assert marker is not None
    assert marker.evaluate(_X64_WIN) is True
    # Linux aarch64 has manylinux wheels, and reports "aarch64", not "ARM64".
    assert marker.evaluate(_LINUX_ARM) is True


def test_no_other_core_requirement_is_arm64_gated() -> None:
    """Guard against silently dropping more packages on ARM64.

    ``qai-appbuilder`` is the one deliberate ARM64-only addition (see
    ``test_pyproject_qai_appbuilder.py``); ``tiktoken`` is the one exclusion.
    """
    gated = [
        name
        for name, r in _core_requirements().items()
        if r.marker is not None and "ARM64" in str(r.marker)
    ]
    assert sorted(gated) == ["qai-appbuilder", "tiktoken"]
