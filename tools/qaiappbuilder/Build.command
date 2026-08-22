#!/bin/bash
# =============================================================================
# Build.command  —  编译 QAIModelBuilder 的 Vue/Vite 前端（macOS 版）
#
# 等价复刻 Windows 的 Setup.bat Step 5b（Node + pnpm 工具链）+ Build.bat：
#   1. 确保 Node.js >= 22（优先用本机 managed Node 22.22.2）
#   2. 通过 corepack 启用 pnpm 11.9.0（与 frontend/package.json 的
#      "packageManager": "pnpm@11.9.0" 严格对齐）
#   3. cd frontend/ 执行  pnpm install  &&  pnpm build
#      （pnpm build = "vue-tsc --noEmit && vite build"，产物在 frontend/dist）
#
# 双击本文件即可在 Terminal 中执行；首次会下载依赖，耗时较长。
# =============================================================================

# 出错即停（corepack 等可失败步骤已单独兜底）
set -euo pipefail

# ---- 定位仓库根目录（本文件位于 qai-appbuilder/ 根） ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/tools/qaimodelbuilder/frontend"
MANAGED_NODE_BIN="/Users/zhuxiaodong/.workbuddy/binaries/node/versions/22.22.2/bin"

echo "============================================================"
echo " QAIModelBuilder — 前端构建 (Vue 3 + Vite)"
echo " 仓库根 : $SCRIPT_DIR"
echo " 前端目录: $FRONTEND_DIR"
echo "============================================================"

# ---- 确保 Node.js >= 22 在 PATH 上 ----
# 若当前 shell 没找到 node，则把 managed Node 22 的 bin 加进来
if ! command -v node >/dev/null 2>&1; then
  if [ -x "$MANAGED_NODE_BIN/node" ]; then
    export PATH="$MANAGED_NODE_BIN:$PATH"
  fi
fi

if ! command -v node >/dev/null 2>&1; then
  echo "[ERROR] 未找到 Node.js。请先安装 Node >= 22（brew install node 或访问 nodejs.org）。"
  exit 1
fi

NODE_VER="$(node --version | sed 's/^v//')"
echo "[INFO] 使用 Node.js v$NODE_VER  ($(command -v node))"

# ---- 确保 pnpm 可用（corepack 启用 pnpm@11.9.0） ----
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
if ! command -v pnpm >/dev/null 2>&1; then
  echo "[INFO] 通过 corepack 启用 pnpm@11.9.0 ..."
  # corepack 可能在 managed Node 的 bin 目录里
  COREPACK_BIN="$(command -v corepack || true)"
  if [ -z "$COREPACK_BIN" ] && [ -x "$MANAGED_NODE_BIN/corepack" ]; then
    COREPACK_BIN="$MANAGED_NODE_BIN/corepack"
    export PATH="$MANAGED_NODE_BIN:$PATH"
  fi
  if [ -z "$COREPACK_BIN" ]; then
    echo "[ERROR] 找不到 corepack，无法启用 pnpm。请确认 Node 安装完整。"
    exit 1
  fi
  "$COREPACK_BIN" enable >/dev/null 2>&1 || true
  "$COREPACK_BIN" prepare pnpm@11.9.0 --activate 2>&1 | tail -3
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "[ERROR] pnpm 仍不可用。可手动运行: corepack enable pnpm"
  exit 1
fi
echo "[INFO] 使用 pnpm $(pnpm --version)"

# ---- 安装依赖 ----
cd "$FRONTEND_DIR"
echo
echo "[INFO] 安装前端依赖 (pnpm install) ..."
pnpm install

# ---- 构建 ----
echo
echo "[INFO] 构建 Vue/Vite 前端 (pnpm build => vue-tsc --noEmit && vite build) ..."
pnpm build

echo
echo "============================================================"
echo " [OK] 前端构建完成。产物目录: $FRONTEND_DIR/dist"
echo "============================================================"

# 保持窗口打开，方便查看结果
exec </dev/tty
read -n 1 -s -r -p "按任意键关闭窗口 ..."
