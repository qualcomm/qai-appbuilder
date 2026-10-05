# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC_CMAKE = (ROOT / "src" / "CMakeLists.txt").read_text(encoding="utf-8")
PYBIND_CMAKE = (ROOT / "pybind" / "CMakeLists.txt").read_text(encoding="utf-8")
ANDROID_MK = (ROOT / "make" / "Android.mk").read_text(encoding="utf-8")
SETUP_PY = (ROOT / "setup.py").read_text(encoding="utf-8")
INIT_PY = (ROOT / "script" / "qai_appbuilder" / "__init__.py").read_text(encoding="utf-8")
CI_YML = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


REQUIRED_LIBRARIES = (
    "EXECUTORCH_MODULE_LIBRARY",
    "EXECUTORCH_TENSOR_LIBRARY",
    "EXECUTORCH_DATA_LOADER_LIBRARY",
    "EXECUTORCH_FLAT_TENSOR_LIBRARY",
    "EXECUTORCH_NAMED_DATA_LIBRARY",
    "EXECUTORCH_EVALUE_LIBRARY",
    "EXECUTORCH_THREADPOOL_LIBRARY",
    "EXECUTORCH_RUNNER_LIBRARY",
    "EXECUTORCH_CORE_LIBRARY",
    "EXECUTORCH_PORTABLE_OPS_LIBRARY",
    "EXECUTORCH_PORTABLE_KERNELS_LIBRARY",
    "EXECUTORCH_KERNELS_UTIL_LIBRARY",
)


def _cmake_required_library_check(cmake: str) -> str:
    marker = "if (NOT EXECUTORCH_MODULE_LIBRARY"
    start = cmake.index(marker)
    return cmake[start : cmake.index("endif()", start)]


def test_executorch_is_rejected_on_windows_before_sdk_validation() -> None:
    expected = (
        "if (APPBUILDER_ENABLE_EXECUTORCH)\n"
        "  if (NOT CMAKE_SYSTEM_NAME STREQUAL \"Linux\" AND\n"
        "      NOT CMAKE_SYSTEM_NAME STREQUAL \"Android\")"
    )
    assert expected in SRC_CMAKE
    assert (
        "if (APPBUILDER_ENABLE_EXECUTORCH)\n"
        "    if (NOT CMAKE_SYSTEM_NAME STREQUAL \"Linux\" AND\n"
        "        NOT CMAKE_SYSTEM_NAME STREQUAL \"Android\")"
    ) in PYBIND_CMAKE


def test_executorch_is_limited_to_linux_or_android_arm64() -> None:
    for cmake in (SRC_CMAKE, PYBIND_CMAKE):
        assert 'CMAKE_SYSTEM_NAME STREQUAL "Linux"' in cmake
        assert 'CMAKE_SYSTEM_NAME STREQUAL "Android"' in cmake
        assert 'CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|AARCH64|ARM64)$"' in cmake


def test_all_required_executorch_libraries_are_checked() -> None:
    for cmake in (SRC_CMAKE, PYBIND_CMAKE):
        check = _cmake_required_library_check(cmake)
        for library in REQUIRED_LIBRARIES:
            assert f"NOT {library}" in check


def test_backend_archives_are_whole_archived_in_cmake() -> None:
    start = SRC_CMAKE.index("set(EXECUTORCH_LIBRARIES")
    link_block = SRC_CMAKE[start : SRC_CMAKE.index("target_link_libraries", start)]
    assert link_block.count("-Wl,--whole-archive") == 3
    assert link_block.count("-Wl,--no-whole-archive") == 3
    assert link_block.count("-Wl,--no-as-needed") == 3
    assert link_block.count("-Wl,--as-needed") == 3
    assert link_block.index("${EXECUTORCH_PORTABLE_OPS_LIBRARY}") < link_block.index("${EXECUTORCH_PORTABLE_KERNELS_LIBRARY}")
    assert link_block.index("${EXECUTORCH_XNNPACK_LIBRARY}") > link_block.index("-Wl,--no-as-needed")
    assert link_block.index("${EXECUTORCH_QNN_LIBRARY}") > link_block.rindex("-Wl,--no-as-needed")


def test_backend_archives_are_whole_archived_in_android() -> None:
    start = ANDROID_MK.index("EXECUTORCH_LINK_LIBS :=")
    link_block = ANDROID_MK[start : ANDROID_MK.index("#==========================", start)]
    assert link_block.count("-Wl,--whole-archive") == 3
    assert link_block.count("-Wl,--no-whole-archive") == 3
    assert link_block.count("-Wl,--no-as-needed") == 3
    assert link_block.count("-Wl,--as-needed") == 3
    assert link_block.index("-lportable_ops_lib") < link_block.index("-lportable_kernels")
    assert link_block.index("-lexecutorch_backend_xnnpack") > link_block.index("-Wl,--no-as-needed")
    assert link_block.index("-lqnn_executorch_backend") > link_block.rindex("-Wl,--no-as-needed")


def test_executorch_runtime_libraries_use_package_rpath() -> None:
    assert 'BUILD_RPATH "$ENV{EXECUTORCH_ROOT}/lib;$ORIGIN/libs"' in SRC_CMAKE
    assert 'INSTALL_RPATH "$ORIGIN/libs"' in SRC_CMAKE
    assert "BUILD_WITH_INSTALL_RPATH TRUE" not in SRC_CMAKE


def test_packaging_covers_versioned_and_transitive_executorch_libraries() -> None:
    for name in ("pthreadpool", "cpuinfo", "quantized_ops_lib", "quantized_kernels"):
        assert name in SETUP_PY
    assert "glob" in SETUP_PY
    assert ".so*" in SETUP_PY
    assert ".dll*" in SETUP_PY
    for name in ("pthreadpool", "cpuinfo", "quantized_ops_lib", "quantized_kernels"):
        assert name in INIT_PY
    assert "glob" in INIT_PY


def test_ci_contains_gated_real_executorch_job() -> None:
    assert "EXECUTORCH_SDK_URL" in CI_YML
    assert "APPBUILDER_ENABLE_EXECUTORCH: 'ON'" in CI_YML
    assert "python -m pytest tests/test_executorch_sdk_smoke.py" in CI_YML
