from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# 🧠 项目配置
PROJECT_NAME = "RagSaveDoc"
ENTRY_POINT = "RagSaveDoc.py"
MODEL_LIST_FILE = "assets/models.yaml"
ICON_PATH = "assets/RagSaveDoc.png"
SPEC_FILE = f"{PROJECT_NAME}.spec"

binaries = []

# ✅ 图标资源
datas = []
if Path(ICON_PATH).exists():
    datas.append((ICON_PATH, "assets"))
    datas.append((MODEL_LIST_FILE, "assets"))

# ✅ chromadb 模块相关的隐藏导入
hiddenimports = [
    *collect_submodules("transformers.models.t5gemma"),
    "transformers.models.smollm3",
    "transformers.models.glm4v",
    "transformers.models.gemma3n",
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
print("💡 你可以运行：pyinstaller RagSaveDoc.spec")