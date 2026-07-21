# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""安装阶段聚合安装 App Builder Pack 依赖（V1 对齐修复 D-1，瘦入口）。

由 ``Setup.bat`` Step 4e 调用（统一 ``qai install-pack-deps`` 子命令；
CLI D3 2026-06-10 之前为独立 console-script ``qai-install-pack-deps``，
现已通过 ``apps/cli/commands/install.py`` 在 ``qai`` dispatcher 暴露），
也可在装机后手动重跑。等价于 V1
``setup_qairt_env.py --install-inference-deps`` 末尾自动触发的
``install_app_builder_deps()``，但 **去掉了 QAIRT / VS / venv_310 等
model-builder 重型耦合**——本入口只做一件事：把四个 App Builder Pack
（whisper-base / zipformer-zh / melotts-zh / ppocrv4）的 ``requirements.txt``
聚合、去重、装进当前 ARM64 venv。

行为对齐 V1：

* 单包安装失败 **仅 warn、整体非致命**（返回 0），不阻断 setup；
* ``qai-appbuilder`` 由 vendor wheel 处理，聚合时跳过（见 :mod:`_pack_deps`）；
* ``openai-whisper`` 以 ``--no-deps`` 安装（修正 V1 既有缺陷，见 :mod:`_pack_deps`）。

跨平台前瞻：纯 stdlib + uv/pip 子进程；无 Windows 专属调用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    """仓库根 = 本文件的上两级（scripts/setup/<this> -> repo root）。"""
    return Path(__file__).resolve().parents[2]


def _load_aggregate():
    """import :func:`scripts.setup._pack_deps.aggregate`，含直接运行兜底。

    优先包内绝对导入；当以 ``python path/to/install_app_builder_deps.py``
    直接运行（``scripts`` 不在 ``sys.path``）时，把仓库根插入 ``sys.path``
    后重试。
    """
    try:
        from scripts.setup._pack_deps import aggregate  # noqa: PLC0415
    except ModuleNotFoundError:
        sys.path.insert(0, str(_repo_root()))
        from scripts.setup._pack_deps import aggregate  # noqa: PLC0415
    return aggregate


def main(argv: list[str] | None = None) -> int:
    aggregate = _load_aggregate()

    root = _repo_root()
    pack_root = root / "factory" / "app_builder" / "models"
    normal, no_deps, sources = aggregate(pack_root)

    if not normal and not no_deps:
        print(
            "[INFO] No App Builder Pack requirements found "
            f"under {pack_root} — nothing to install."
        )
        return 0

    python = sys.executable  # Setup.bat 已 activate ARM64 venv
    uv = root / "data" / "bin" / "uv" / "uv.exe"
    whl = root / "vendor" / "whl"

    def _pip(args: list[str]) -> bool:
        if uv.is_file():
            cmd = [str(uv), "pip", "install", "--python", python, *args]
        else:
            cmd = [python, "-m", "pip", "install", *args]
        if whl.is_dir():
            cmd += ["--find-links", str(whl)]
        print("[INFO] " + " ".join(cmd))
        try:
            return subprocess.run(cmd, check=False).returncode == 0
        except OSError as exc:  # spawn 失败也非致命
            print(f"[WARN] failed to spawn installer: {exc}")
            return False

    failed: list[str] = []

    # 1) --no-deps 包（修正 D-2：openai-whisper 等）
    for spec in no_deps:
        if not _pip(["--no-deps", spec]):
            failed.append(spec)

    # 2) 普通包：先批量，失败再逐个（对齐 V1）
    if normal and not _pip(list(normal)):
        print("[WARN] Batch install failed; retrying one-by-one...")
        for spec in normal:
            if not _pip([spec]):
                failed.append(spec)

    total = len(normal) + len(no_deps)
    if failed:
        print(
            f"[WARN] App Builder Pack deps installed with {len(failed)} "
            f"failure(s): {total - len(failed)}/{total} packages from "
            f"{len(sources)} Packs (failed: {', '.join(failed)})"
        )
    else:
        print(
            f"[OK] App Builder Pack deps installed: {total} packages from "
            f"{len(sources)} Packs ({', '.join(sources)})"
        )

    # 永远非致命（对齐 V1：单包失败不阻断 setup）。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
