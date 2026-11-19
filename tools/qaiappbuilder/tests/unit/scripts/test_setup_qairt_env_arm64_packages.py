# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Windows ARM64's inference venv takes its post-processing packages from PyPI.

``scripts/setup/setup_qairt_env.py`` used to install opencv / MarkupSafe / soundfile /
kaldi-native-fbank / pyclipper from ``vendor/whl`` (vendor-deps.7z) and only warned
"wheel not found - skipping" when the bundle was absent, which silently left the venv
without them. They are plain PyPI packages now (native cp313 ``win_arm64`` wheels).
The function is platform-branched, so this checks the source of the ARM64 branch.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parents[3] / "scripts" / "setup" / "setup_qairt_env.py").read_text(
    encoding="utf-8"
)


def _arm64_branch() -> str:
    start = _SRC.index('    elif _detect_arch() == "aarch64":')
    end = _SRC.index("        # ── Windows x64", start)
    return _SRC[start:end]


def test_no_code_path_reads_the_vendored_wheel_directory() -> None:
    code = [ln for ln in _SRC.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("arm64_local_wheels" in ln or '"vendor" / "whl"' in ln for ln in code)


def test_arm64_installs_the_post_processing_packages_from_pypi() -> None:
    branch = _arm64_branch()
    for spec in (
        '"opencv-python-headless"',
        '"MarkupSafe"',
        '"soundfile>=0.14.0"',
        '"kaldi-native-fbank"',
    ):
        assert spec in branch, spec


def test_pyclipper_is_not_installed_on_arm64() -> None:
    code = [ln for ln in _arm64_branch().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("pyclipper" in ln for ln in code)


def test_x64_branch_is_unchanged() -> None:
    x64 = _SRC[_SRC.index("        # ── Windows x64") :]
    assert '"pyclipper"' in x64 and '"opencv-python-headless"' in x64
