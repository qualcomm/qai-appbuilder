# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
from pathlib import Path
from glob import glob
import site
from PyInstaller.utils.hooks import collect_submodules

# 🧠 项目配置
PROJECT_NAME = "GenieFletUI"
ENTRY_POINT = "GenieFletUI.py"
MODEL_LIST_FILE = "assets/models.yaml"
ICON_PATH = "assets/GenieFletUI.png"
SPEC_FILE = f"{PROJECT_NAME}.spec"

# ✅ 自动查找 llama.dll 路径（仅限 Windows + site-packages 安装）
llama_dll_path = None
for site_path in site.getsitepackages():
    candidate = Path(site_path) / "llama_cpp" / "lib" / "llama.dll"
    if candidate.exists():
        llama_dll_path = candidate
        break

binaries = []
if llama_dll_path:
    binaries.append((str(llama_dll_path), "llama_cpp/lib"))
    print(f"✅ 检测到 llama.dll: {llama_dll_path}")
else:
    print("⚠️ 未检测到 llama.dll，跳过 DLL 打包")

# ✅ 图标资源
datas = []
if Path(ICON_PATH).exists():
    datas.append((ICON_PATH, "assets"))
    datas.append((MODEL_LIST_FILE, "assets"))

# ✅ chromadb 模块相关的隐藏导入
hiddenimports = [
    *collect_submodules("transformers.models.t5gemma"),
    "transformers.models.smollm3",
    "chromadb.telemetry.product.posthog",
    "analytics",
    "dateutil.tz",
    "chromadb.segment.impl.metadata.sqlite",
    "chromadb.execution.executor.local",
    "chromadb.api.rust"
]

# ✅ spec 内容模板
spec_content = f"""
# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ["{ENTRY_POINT}"],
    pathex=["."],
    binaries={binaries},
    datas={datas},
    hiddenimports={hiddenimports},
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="{PROJECT_NAME}",
    debug=False,
    base="Win32GUI",
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    onefile=True,
    console=False,
    icon="{ICON_PATH}"
)
"""

# ✅ 写入文件
with open(SPEC_FILE, "w", encoding="utf-8") as f:
    f.write(spec_content.strip())

print(f"\n✅ .spec 文件生成成功：{SPEC_FILE}")
print("💡 你可以运行：pyinstaller GenieFletUI.spec")