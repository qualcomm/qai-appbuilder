# 编译与运行 快速指南

> QAI AppBuilder 的安装、构建、运行与打包速查表。
>
> 英文版：[`QUICK-START.md`](QUICK-START.md)。

> **平台前提**：Windows on Snapdragon（ARM64）。所有 `.bat` 均在仓库根目录双击或在
> `cmd.exe` 里执行。脚本会自动下载 uv / Python 3.13 ARM64 / PortableGit / Node.js
> 等工具到 `%LOCALAPPDATA%\QAIModelBuilder\`，**无需管理员权限、无需手装 Python**。

---

## 只想用，不改代码？

```cmd
Setup.bat
Start.bat
```

就这两步。`Setup.bat` 自动安装所有依赖（仅首次）；`Start.bat` 启动 WebUI 并自动打开浏览器。
开始对话即可构建应用、转换模型，或从 AI Hub 下载运行。

> **第一个界面是登录。** WebUI 由 Okta 单点登录把关（默认开启），先登录才能进入工具。
> 仅本地 `pnpm dev` 时才建议关闭。

---

## 开发者

`Setup.bat` 准备本地环境；使用 `Build.bat` 更新前端，再用 `Start.bat` 运行应用。

---

## A. 开发模式（最常用）

### 第一次：一键装环境

```cmd
Setup.bat
```

会做完：下载 uv / 安装 Python 3.13（默认 ARM64，`--arch x64` 时装 x64 版）/ 建 venv 到
`%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313`（或 `.venv_x64_313`）/
安装 `pyproject.toml` 全部依赖 / 装 PortableGit + Node.js + pnpm / 装 QAIRT SDK（模型转换需要，
~2 GB）/ 预下载 Whisper / Zipformer / MeloTTS 模型权重 / 初始化 `data/`（`qai.db`、出厂种子、secret 命名空间）。

常用开关：

| 开关 | 用途 |
|---|---|
| `--arch arm64\|x64` | 强制指定架构（默认自动探测）。在 WoS 上装 x64 栈用于验证时传 `--arch x64` |
| `--no-builder` | 跳过 QAIRT SDK / VS 工具链（不做模型转换时省 ~2 GB） |
| `--dev` | 额外装贡献者工具链（pytest / mypy / ruff / playwright + Chromium） |
| `--no-pause` | 安装完不暂停（CI 调用时用） |

> 一次跑通即可，幂等可重复跑。**`data/` 不在仓库里**，删了重跑 `Setup.bat` 即可重生。

### 编译前端

后端是 Python 解释执行，**不需要编译**；前端 Vue/Vite 需要构建到 `frontend\dist\` 才能被 `Start.bat` 提供：

```cmd
Build.bat              REM 快速增量：仅 vite build（开发迭代用）
Build.bat --full       REM 完整：gen:types + typecheck + lint + test + build（提交/发布前用）
Build.bat --clean      REM node_modules 损坏时：清空重装
```

> **改后端 Python**：直接 `Start.bat` 重启即可，不用跑 `Build.bat`。
> **改前端 Vue/TS**：跑一次 `Build.bat` 再 `Start.bat`。

### 启动服务

```cmd
Start.bat
```

Server 启动时自动选取可用端口，实际 URL 写入
`data\runtime\server.endpoint.json`，浏览器会自动打开正确地址。按 `Ctrl+C` 停止。

### 其他便捷入口

| 命令 | 用途 |
|---|---|
| `qai.bat <args>` | 不进 venv 直接跑 CLI（`qai --help` / `qai config provider list` / `qai build`…） |
| `Console.bat` | 双击进入激活好的 venv 交互 shell，用 `pip install <pkg>` 装额外包 / 跑 ad-hoc Python |
| `Uninstall.bat` | 卸载 Setup.bat 装到项目外的东西（venv / PortableGit / Node）；**不动 `data/`** |
| `Uninstall.bat --all` | 上面 + uv 缓存 + QAIRT SDK + Playwright Chromium + `vendor/` 运行时缓存 |

---

## 打包给最终用户

先构建前端，再创建发布压缩包：

```cmd
Build.bat --install
```

将完整的 `tools\qaiappbuilder\` 目录压缩为 **`qaiappbuilder.zip`**。不要包含本地运行时产物，例如 `data\`、`frontend\node_modules\` 或临时构建缓存。

**最终用户安装流程：**

```cmd
解压 qaiappbuilder.zip  →  Setup.bat  →  Start.bat
```

> 最终用户机器无需预装 Python、Node.js 或 Git；`Setup.bat` 会安装所需运行时工具。

---

## 速查：我现在要做什么？

| 你要做的事 | 跑什么 |
|---|---|
| 第一次拉源码下来 | `Setup.bat` |
| 改了 Python 后端 | `Start.bat`（重启即可） |
| 改了 Vue/TS 前端 | `Build.bat` 然后 `Start.bat` |
| 改了前端依赖 (`package.json`) | `Build.bat --install` |
| `node_modules` 坏了 | `Build.bat --clean` |
| 写贡献者测试 / 跑 pytest | `Setup.bat --dev` 一次，之后 `Console.bat` 进 venv 跑测试 |
| 打包给最终用户 | `Build.bat --install`，然后将 `tools\qaiappbuilder\` 压缩为 `qaiappbuilder.zip` |
| 一次性敲 CLI 命令 | `qai.bat <args>` |
| 装额外 Python 包临时试试 | `Console.bat` 进 venv，`pip install <pkg>` |
| 彻底清理（保留 `data/`） | `Uninstall.bat`（或 `--all` 更深度） |

> **`Setup.bat` / `Build.bat` / `Uninstall.bat` 支持 `--help` / `-h` / `/?`** ——例如 `Build.bat --help` 看全部开关。`Start.bat` / `qai.bat` 会将额外参数透传给底层 Python 入口。
