# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Patch qairt_env.json so runtime venv + subdir match the current host arch.

Usage:
    python patch_qairt_env_arch.py <qairt_env.json> <host_arch>

Where <host_arch> is "arm64" or "x64" (lowercase, as set by Setup.bat).

This fixes the case where qairt_env.json was generated on a different-arch
machine (e.g. ARM64 WoS) and then the same install tree is used on an x64
host (shared network path, copied deployment). Without this patch the
App Builder persistent runner would launch the ARM64 Python interpreter on
an x64 machine (or vice-versa).

Idempotent: exits silently when values already match.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: patch_qairt_env_arch.py <qairt_env.json> <host_arch>", file=sys.stderr)
        return 1

    cfg_path = sys.argv[1]
    host_arch = sys.argv[2].lower()

    if host_arch not in ("arm64", "x64"):
        print(f"[ERROR] Invalid host_arch: {host_arch!r} (expected arm64 or x64)", file=sys.stderr)
        return 1

    if not os.path.isfile(cfg_path):
        # Nothing to patch — Step 8.6 hasn't run yet.
        return 0

    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)

    # Determine expected values for this arch.
    venv_name = ".venv_arm64_313" if host_arch == "arm64" else ".venv_x64_313"
    subdir = "arm64x-windows-msvc" if host_arch == "arm64" else "x86_64-windows-msvc"
    arch_val = "aarch64" if host_arch == "arm64" else "x86_64"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    expected_venv = os.path.join(local_app_data, "QAIModelBuilder", "envs", venv_name)

    changed = False

    # --- python_runtime_venv ---
    cur_rv = data.get("python_runtime_venv", "")
    if cur_rv:
        # Normalise separators for comparison.
        normalised = cur_rv.replace("\\", "/")
        if not normalised.endswith(venv_name):
            data["python_runtime_venv"] = expected_venv
            changed = True
    # If key is absent, don't add it (Step 8.6 generates the full file).

    # --- python_arm64_venv (legacy key, also read by QairtEnvJsonResolver) ---
    cur_arm64 = data.get("python_arm64_venv", "")
    if cur_arm64 and host_arch == "x64":
        # On x64, ensure python_runtime_venv is set correctly; arm64 key stays
        # as documentation but runtime_venv takes priority in resolver.
        if "python_runtime_venv" not in data or data["python_runtime_venv"] != expected_venv:
            data["python_runtime_venv"] = expected_venv
            changed = True

    # --- qairt_runtime_subdir ---
    if data.get("qairt_runtime_subdir", "") != subdir:
        data["qairt_runtime_subdir"] = subdir
        changed = True

    # --- arch ---
    if data.get("arch", "") != arch_val:
        data["arch"] = arch_val
        changed = True

    if changed:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[OK]   qairt_env.json patched: runtime_venv -> {venv_name}, subdir -> {subdir}")
    else:
        print(f"[SKIP] qairt_env.json already correct for {host_arch}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
