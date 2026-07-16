# 编译与运行 快速指南

> 三种使用模式 + 一张速查表。
>
> 英文版：[`QUICK-START.md`](QUICK-START.md)。

> **平台前提**：Windows on Snapdragon（ARM64）。所有 `.bat` 均在仓库根目录双击或在
> `cmd.exe` 里执行。脚本会自动下载 uv / Python 3.13 ARM64 / PortableGit / Node.js
> 等工具到 `%LOCALAPPDATA%\QAIModelBuilder\`，**无需管理员权限、无需手装 Python**。

---

## 一图看懂三种模式

| 模式 | 用什么 | 适合谁 | 第一次准备 | 日常启动 |
|---|---|---|---|---|
| **A. 开发模式（源码运行）** | `Setup.bat` → `Build.bat` → `Start.bat` | 改代码 / 调试 / 贡献者 | `Setup.bat` + `Build.bat` | `Start.bat`（改后端）/ `Build.bat` + `Start.bat`（改前端） |
| **B. 桌面 App（Tauri）** | `Setup.bat --desktop` → `Build.bat --desktop` | 想要一个独立 .exe / .msi 安装包 | `Setup.bat --desktop` | 运行 `desktop\src-tauri\target\release\bundle\` 下的安装包 |
| **C. 发布包（External / Internal）** | `Release.bat [版本号] [--internal]` | 打包给最终用户 | `Setup.bat` 一次 | `dist\release\` 下的归档 → 解压 → 跑 `Setup.bat` → `Start.bat` |

---

## A. 开发模式（最常用）

### 第一次：一键装环境

```cmd
Setup.bat
```

会做完：下载 uv / 安装 Python 3.13 ARM64 / 建 venv 到 `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313` /
安装 `pyproject.toml` 全部依赖 / 装 PortableGit + Node.js + pnpm / 装 QAIRT SDK（模型转换需要，
~2 GB）/ 预下载 Whisper / Zipformer / MeloTTS 模型权重 / 初始化 `data/`（`qai.db`、出厂种子、secret 命名空间）。

常用开关：

| 开关 | 用途 |
|---|---|
| `--no-builder` | 跳过 QAIRT SDK / VS 工具链（不做模型转换时省 ~2 GB） |
| `--dev` | 额外装贡献者工具链（pytest / mypy / ruff / playwright + Chromium） |
| `--desktop` | 额外装 Rust + tauri-cli + WebView2，准备桌面 App 构建 |
| `--no-pause` | 安装完不暂停（CI 调用时用） |

> 一次跑通即可，幂等可重复跑。**`data/` 不在仓库里**，删了重跑 `Setup.bat` 即可重生。

### 编译前端

后端是 Python 解释执行，**不需要编译**；前端 Vue/Vite 需要构建到 `frontend\dist\` 才能被 `Start.bat` 提供：

```cmd
Build.bat              REM 快速增量：仅 vite build（开发迭代用）
Build.bat --full       REM 完整：gen:types + typecheck + lint + test + build（提交/发布前用）
Build.bat --clean      REM node_modules 损坏时：清空重装
Build.bat --desktop    REM 顺带打 Tauri 桌面 release 安装包
Build.bat --desktop-dev REM 调试桌面壳（`cargo tauri dev` 热重载）
```

> **改后端 Python**：直接 `Start.bat` 重启即可，不用跑 `Build.bat`。
> **改前端 Vue/TS**：跑一次 `Build.bat` 再 `Start.bat`。

### 启动服务

```cmd
Start.bat              REM 普通模式：新窗口启动 server，自动开浏览器
Start.bat --reload     REM 热重载（开发调试）
```

端口动态选择（默认 8989，被占就回退到 8088 / 7799 / 12989 / 18989 / 28989），实际 URL 写入 `data\runtime\server.endpoint.json`，浏览器会自动打开正确地址。Ctrl+C 即停。

### 其他便捷入口

| 命令 | 用途 |
|---|---|
| `qai.bat <args>` | 不进 venv 直接跑 CLI（`qai --help` / `qai config provider list` / `qai build`…） |
| `Console.bat` | 双击进入激活好的 venv 交互 shell，用 `pip install <pkg>` 装额外包 / 跑 ad-hoc Python |
| `Uninstall.bat` | 卸载 Setup.bat 装到项目外的东西（venv / PortableGit / Node）；**不动 `data/`** |
| `Uninstall.bat --all` | 上面 + uv 缓存 + QAIRT SDK + Playwright Chromium + `vendor/` 运行时缓存 |

---

## B. 桌面 App（Tauri）

把 WebUI 打包成独立 `.exe` / `.msi`，用户不必看到浏览器。

```cmd
Setup.bat --desktop          REM 第一次：装 Rust + tauri-cli + WebView2 运行时
Build.bat --desktop          REM 出 release 安装包
```

产物位置：`desktop\src-tauri\target\release\bundle\`（`.msi` / `.exe`）。

本地调试桌面壳（不出包）：

```cmd
Build.bat --desktop-dev      REM 启动 cargo tauri dev，热重载 Rust 改动
```


---

## C. 发布包（给最终用户）

`Release.bat` 跑完整流水线：clean → 前端 build → factory 编译 → assemble → 写
`build_info.json` → 清洗 internal-only 资产（external 时） → manifest 白名单校验 → 归档。

```cmd
Release.bat                  REM 默认：external 版，版本号 2.0.0
Release.bat 2.1.0            REM external 版，指定版本号
Release.bat --internal       REM internal 全功能版（保留内部 provider / 上报）
Release.bat 2.1.0 --internal REM 组合
```

产物：`dist\release\` 下的目录 + 归档；`build_info.json` 自报版本和 edition。

**用户机器上的安装流程**（参考——这是发布包发给终端用户后他们要做的）：

```cmd
解压发布包  →  Setup.bat  →  Start.bat
```

> 用户机不需要 Python / Node / git，`Setup.bat` 全自动搞定。

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
| 想要桌面 App | `Setup.bat --desktop` + `Build.bat --desktop` |
| 打发布包给用户 | `Release.bat [版本号]` |
| 一次性敲 CLI 命令 | `qai.bat <args>` |
| 装额外 Python 包临时试试 | `Console.bat` 进 venv，`pip install <pkg>` |
| 彻底清理（保留 `data/`） | `Uninstall.bat`（或 `--all` 更深度） |

> **任何脚本都支持 `--help` / `-h` / `/?`**，例如 `Build.bat --help` 看全部开关。
