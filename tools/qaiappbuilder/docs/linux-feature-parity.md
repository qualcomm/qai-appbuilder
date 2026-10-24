# QAI AppBuilder Linux 平台功能对等表

> **文档目的**：从应用层（用户视角）梳理 QAI AppBuilder 在 Linux 上提供的功能，以及与 Windows 的差异，作为 Linux 测试范围、缺陷跟踪和客户文档的基础。
>
> **依据与局限**：本文基于代码阅读与 README 整理，**尚未在 Linux 上实际运行验证**。README 中明确测试过的平台只有 Windows on Snapdragon（X Elite）。
>
> **整理日期**：2026-09-29

## 图例

| 标记 | 含义 |
|---|---|
| ✅ 支持 | 功能与平台无关，或代码中有明确的 Linux/POSIX 实现 |
| ⚠️ 待验证/受限 | 代码中有 Linux 分支但缺少测试证据，或需额外手动步骤，或能力弱于 Windows |
| ❌ 不支持 | 仅有 Windows 实现，Linux 上不可用 |

## 目标环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04 |
| 架构 | aarch64（QCS8300 等 Snapdragon IoT 开发板）、x86_64 |
| NPU 依赖 | aarch64 需安装 `qcom-fastrpc1`（提供 `libcdsprpc.so`，`setup.sh` 会检测并补建软链接） |
| Python | 系统预装 3.12（`python3.12`、`python3.12-venv`；aarch64 另需 `python3.12-dev`） |
| Node.js / pnpm | Node.js ≥ 22、pnpm ≥ 9（使用 `--no-frontend` 可跳过） |
| 编译工具（仅 aarch64） | `cmake`、`build-essential`（从源码编译 onnxsim 0.4.36） |
| QAIRT SDK | `setup.sh` 自动安装到 `~/qairt/<version>`（或 `$QAIRT_SDK_ROOT`） |

---

## 1. 安装与运行

| 功能 | Windows | Linux | 说明 |
|---|---|---|---|
| 安装 | `Setup.bat` | ✅ `bash setup.sh` | Windows 自动下载 uv、Python、Git、Node；Linux 复用系统已有的 Python、Node、pnpm。Linux 只有一个参数 `--no-frontend` |
| 启动 | `Start.bat` | ✅ `bash start.sh [--port N]` | Linux 前台运行，按 Ctrl+C 停止；不自动打开浏览器，不支持 `--reload` |
| 访问方式 | 浏览器 / 桌面应用 | ✅ 浏览器 | 实际地址写在 `data/runtime/server.endpoint.json` |
| 前端构建 | `Build.bat` | ⚠️ 无单独脚本 | 重跑 `setup.sh`，或手动执行 `pnpm -C frontend build` |
| 桌面应用 | ✅（WebView2） | ❌ | |
| 卸载 / 升级 | ✅ `Uninstall.bat` | ❌ | `scripts/init/uninstall.py` 按 Windows 设计，Linux 上大部分步骤直接跳过（macOS 另有 `Uninstall.command`）；Linux 没有卸载脚本，也没有升级和数据迁移流程 |
| 服务化运行（开机自启、崩溃重启） | — | ❌ | 没有 systemd unit |
| 用户登录（Okta SSO） | ✅ 默认开启 | ⚠️ | Linux 上是否开启取决于是否有 `DISPLAY`。无显示器的开发板上默认不认证，且 OIDC 回调依赖本地浏览器 |
| 服务监听地址 | 127.0.0.1 | ⚠️ 127.0.0.1 | 开发板通常需要远程访问，需要配套设计认证策略 |

## 2. 对话（Chat）

| 功能 | Linux | 说明 |
|---|---|---|
| WebSocket 流式输出（失败时自动切换到 SSE） | ✅ | |
| 多智能体讨论模式，以及 Agent、Roster、Mode 模板 | ✅ | |
| Markdown、代码高亮、Mermaid 渲染、多标签页对话 | ✅ | 前端功能 |
| 兼容 OpenAI 的接口（`/v1/chat/completions`、`/v1/models`） | ✅ | |
| 云端模型（兼容 OpenAI 的服务商） | ✅ | |
| API 密钥安全存储 | ⚠️ | 默认使用系统密钥环。开发板通常没有 Secret Service，会改用加密文件存储，这条路径需要验证 |
| 本地 NPU 大模型对话 | ❌ | 依赖 GenieAPIService，见第 7 节 |

## 3. 智能体内置工具

| 工具 | Linux | 说明 |
|---|---|---|
| 文件：`read`、`list`、`write`、`edit`、`glob`、`grep`、`apply_patch` | ✅ | |
| Shell：`exec` | ✅ | Linux 上用 sh 执行，同样要经过权限审批 |
| `webfetch` | ✅ | |
| 子智能体：`agent`、`list_subagents` | ✅ | |
| `skill`、`todowrite`、`question` | ✅ | |
| 浏览器自动化 | ⚠️ | 需要安装 Playwright 并在配置中开启 `[browser]` |
| `computer` 桌面操控（鼠标、键盘、截屏） | ❌ | 只有 Win32 实现。默认关闭，但开启时**不检查平台**，Linux 上开启后工具会出现，一调用就报错 |
| MCP 客户端（接入外部 MCP 服务） | ✅ | 代码中有 Linux 分支 |
| 对话事件钩子（Settings → Hooks） | ✅ | 执行 Shell 命令 |
| 定时任务 | ✅ | 与平台无关（`computer` 类任务除外） |

## 4. 技能（Skills）

| 技能 | 类型 | Linux | 说明 |
|---|---|---|---|
| code-assist | 内置 | ✅ | |
| ppt-gen | 内置 | ✅ | |
| model-hub | 内置 | ✅ | |
| model-builder | 内置 | ⚠️ | 见第 6 节 |
| weather、read-arxiv-paper、email-163-com 等 | 用户可安装 | ✅ | |
| browser-automation | 用户可安装 | ⚠️ | 依赖 Playwright |
| computer-automation | 用户可安装 | ❌ | 依赖 `computer` 工具 |
| 技能开关与热重载 | — | ✅ | |
| 按操作系统适配的系统提示词 | — | ✅ | Linux 上会告诉模型：当前是 Ubuntu Linux、模型格式为 ONNX/DLC、没有原生文件拦截 |

## 5. App Builder（在 NPU 上一键运行模型包）

| 功能 | Linux | 说明 |
|---|---|---|
| 扫描模型包、运行、历史记录、对比、基准测试、分享 | ✅ | 应用逻辑 |
| 常驻 Worker（Sticky Worker）、子进程管理 | ✅ | 有 POSIX 实现 |
| 多精度变体切换（FP16 / INT8 / W8A16） | ✅ | |
| LLM 编排多个模型包（`appbuilder_run`） | ✅ | 需要云端模型 |
| 内置模型包：Whisper Base、Zipformer ZH、MeloTTS ZH、Real-ESRGAN | ⚠️ **很可能不能直接用** | context binary 是按 X Elite 的 HTP v73 编译的，与 SoC 绑定。QCS8300 等开发板需要重新转换出对应的模型包 |
| 从 Model Builder 生成新模型包（Promote） | ⚠️ | 流程上支持，未验证 |

## 6. Model Builder（用自然语言驱动模型转换）

| 功能 | Linux | 说明 |
|---|---|---|
| 主机类型识别 | ✅ | 可识别 `linux-aarch64`、`linux-x64` |
| Flow A（`run_pipeline.py`，默认） | ⚠️ | 设计上支持所有主机类型；Linux 上主要是 ONNX → DLC → .bin |
| 编译 DLL 路线（依赖 VS ARM64） | ❌ | 仅适用于 Windows |
| Flow B：SNPE DLC | ⚠️ | |
| Flow C：DLC → bin | ⚠️ | |
| 量化精度（W8A16 / W8A8 / W4A16 等） | ⚠️ | |
| 在开发板上完成转换和推理的全流程 | ⚠️ **未测试** | README 中测过的 Linux 场景都是在 x86 主机上转换，再到 qcs6490 或 RB8 上推理 |
| 已知问题 | — | qcs6490 做 QNN 量化可能报 `SocModel doesn't support FP16`，需要改走 SNPE 流程 |

## 7. 本地 NPU 大模型服务与下载中心

| 功能 | Linux | 说明 |
|---|---|---|
| GenieAPIService 本地大模型服务（启动、停止、加载模型、查看日志、开机自启） | ❌ | 默认程序名写死为 `GenieAPIService.exe`；端口占用回收在 Linux 上直接跳过；安装器探测进程用的是 Windows 的 `wmic` |
| 下载中心：浏览模型清单 | ✅ | |
| 下载中心：aria2c 多线程下载、断点续传、sha256 校验 | ⚠️ | aria2c 只在 Windows 上自动安装，Linux 需要先手动 `apt install aria2` |

## 8. 其他功能

| 功能 | Linux | 说明 |
|---|---|---|
| 远程部署（通过 SSH 部署到开发板） | ✅ | 基于 Paramiko，与平台无关 |
| 外部消息渠道（如飞书） | ✅ | |
| 网络代理设置 | ✅ | |
| 主题切换、中英文界面 | ✅ | |

## 9. 安全防护

| 防护层 | Windows | Linux | 说明 |
|---|---|---|---|
| PolicyCenter 命令策略 | ✅ | ✅ | |
| Protected Paths、FileGuard | ✅ | ✅ | |
| 权限审批流程 | ✅ | ✅ | |
| guard64.dll 系统级文件和进程拦截 | ✅ | ❌ | Linux 上没有替代机制 |
| 子进程沙箱 | — | ❌ | 可考虑 bubblewrap、Landlock 或 seccomp |

> Linux 上智能体执行命令时只有应用层防护，防护强度弱于 Windows。交付客户前需要在文档中明确说明。

---

## 10. 汇总

### 可以交付的核心能力（风险较低）

- 通过浏览器访问 Web UI
- 云端大模型对话（包括多智能体讨论）
- 文件和 Shell 工具、子智能体、技能、MCP
- 远程部署、外部消息渠道

### 价值最大但最缺验证的部分

- App Builder 在 NPU 上运行模型包：需要为目标 SoC 重新生成内置模型包
- 在开发板上用 Model Builder 转换模型：需要在真机上跑通全流程

### 明确缺失

- 本地 NPU 大模型服务（GenieAPIService）
- `computer` 桌面操控工具和 computer-automation 技能
- 桌面应用
- aria2c 自动安装
- 卸载、升级、systemd 服务化
- 系统级安全拦截

### 交付前必须处理的问题

| # | 问题 | 建议 |
|---|---|---|
| 1 | `computer` 工具开启时不检查平台 | 注册工具前检查平台能力，Linux 上不暴露该工具 |
| 2 | GenieAPIService 页面在 Linux 上不可用 | 在 UI 中隐藏，或显示明确的"暂不支持"说明 |
| 3 | 无显示器开发板上默认不做认证 | 重新设计远程访问下的认证方式和监听地址 |
| 4 | 内置模型包与 X Elite 绑定 | 为目标 SoC 提供对应的模型包，或在 UI 中标出兼容的 SoC |
| 5 | aria2c 需要手动安装 | 在 `setup.sh` 中检测或安装，并在 UI 中给出提示 |
| 6 | 安全防护弱于 Windows | 用专用的低权限用户运行服务，评估子进程沙箱，文档中说明安全边界 |

---

## 附录：待验证清单

以下各项需要在 Ubuntu 22.04 / 24.04（aarch64 真机和 x86_64）上逐项验证，并把结果回填到上面各表中：

- [ ] 在干净系统上执行 `setup.sh`（带 / 不带 `--no-frontend`）
- [ ] 执行 `start.sh` 后，健康检查接口正常，浏览器可以访问
- [ ] 在无显示器环境下的认证行为，以及远程访问
- [ ] 无 Secret Service 时的密钥存储与读取
- [ ] 云端模型对话，包括 WebSocket 和 SSE 两种方式
- [ ] `exec` 工具的权限审批、超时，以及进程树回收（确认没有孤儿进程）
- [ ] MCP 客户端接入外部 MCP 服务
- [ ] Playwright 浏览器自动化
- [ ] App Builder 在目标 SoC 上运行重新转换的模型包
- [ ] 在开发板上用 Model Builder 走完 ONNX → DLC → .bin，并完成推理
- [ ] 手动安装 aria2 后，下载中心的下载、断点续传和校验
- [ ] 远程部署到开发板
- [ ] 服务重启后端口释放，以及 `server.endpoint.json` 的更新
