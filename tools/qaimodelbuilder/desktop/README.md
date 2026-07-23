# QAIModelBuilder Desktop 壳（Tauri 2.x）

> **状态**：D4（可跑骨架，已超出最初 PoC）
> **设计依据**：`docs/90-refactor/desktop-app-plan.md` §3
> **架构定位**：桌面 UI 壳（Rust），与项目 Python 环境完全解耦
> **平台**：Windows ARM64（首要） / Windows x64（已支持） / macOS / Linux（未验证）

## 这是什么

一个 **Tauri 2.x** 写成的桌面 UI 壳，作用只有两件：

1. 启动时 **spawn** 一个 `qai-serve` 后端子进程（用工程预装的 venv，按
   `data/config/host_arch` 选择 `.venv_arm64_313` 或 `.venv_x64_313`），让
   FastAPI 跑在自动选的空闲端口上；
2. 打开一个 WebView 窗口指向 `http://127.0.0.1:<port>/`，加载 `frontend/dist/`
   的 SPA。

**它不与后端共进程**——后端始终是独立的 Python 进程，跑在对应架构的 venv
（`.venv_arm64_313` 或 `.venv_x64_313`，取决于 `data/config/host_arch`）
里。壳本身是 Rust 编译产物（`.exe`/`.msi`），**不进 Python venv**，因此完全
规避了 wheel 兼容性风险（详见 `desktop-app-plan.md` §3.3）。

退出时（关窗口）壳先走 HTTP 优雅停止，再兜底强杀（见下「关闭流程」）。

## 前置条件

- Rust 工具链（rustc 1.96+ ARM64）
- Tauri CLI 2.11+：`npm install -g @tauri-apps/cli@^2`
- WebView2 Runtime（Windows 10 1803+ 已默认装配）
- Visual Studio Build Tools 2022（含 MSVC + Windows SDK）
- 工程预装环境：`Setup.bat` 已跑过（生成 `.venv_arm64_313` 或 `--arch x64` 时 `.venv_x64_313`）
- 前端产物：`pnpm -C frontend build` 已跑过（`frontend/dist/` 存在）

## 构建（推荐：一条命令，自动拷到 dist/）

```powershell
# 在仓库根执行；自动加 cargo 到 PATH、编译、把产物拷到 dist/desktop/
.\desktop\build.ps1
# 产物：dist\desktop\QAIModelBuilder.exe (+ .dll)
```

```powershell
# 含 .msi / NSIS 安装包（正式发布形态）
.\desktop\build.ps1 -Bundle
# 额外产物：dist\desktop\bundle\*.msi、*-setup.exe
```

`build.ps1` 是唯一推荐的构建入口——它封装了"编译 + 自动拷贝产物到稳定目录"，
**不需要手动拷贝**。

## 构建产物目录布局

- **编译缓存**：`.build/desktop-target/`（仓库根，~1.4 GB，含中间产物）。
  由 `desktop/src-tauri/.cargo/config.toml` 把 Cargo 的 `target-dir` 重定向到
  这里，使 `desktop/` 源码树保持 ~0.5 MB、便于压缩备份。`cargo clean` 会清它。
- **可运行产物**：`dist/desktop/`（仓库根，仅 .exe + .dll）。由 `build.ps1` 从
  编译缓存拷贝过来——干净、稳定、不被 `cargo clean` 删。与 `frontend/dist/` 同
  语义。
- 两者都在 `.gitignore` 里（`/​.build/` + `/dist/`），不入仓。

> 直接 `cargo build` 也能编译，但产物会留在 `.build/desktop-target/release/`，
> 需手动拷；用 `build.ps1` 省去这一步。
>
> ⚠️ 首次 `cargo build` 会下载/编译 ~500 个 crate，预计 10–30 分钟（ARM64 release）；
> 后续增量编译约 10–20 秒。

## 开发热重启

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
cd desktop\src-tauri
cargo tauri dev
```

## 运行

```powershell
.\dist\desktop\QAIModelBuilder.exe
```

启动顺序：

```
1) shell 进程启动
2) 选一个空闲端口（如 51626）
3) 用 find_repo_root() 从 exe 向上查找含 src/ + apps/ 的目录作为 repo root
   （鲁棒：不依赖固定层数，target 重定向到 .build/ 后仍正确）
4) spawn:  %LOCALAPPDATA%\QAIModelBuilder\envs\<venv>\Scripts\python.exe
           -m apps.cli.serve --port 51626   （CWD = repo root）
   <venv> = .venv_arm64_313 或 .venv_x64_313（由 data/config/host_arch 决定）
   flags: CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
          （后者抑制后端 python 的黑色 console 窗口）
5) 探活：connect 127.0.0.1:51626 直到成功（超时 15s）
6) 打开 WebView 指向 http://127.0.0.1:51626/
7) 用户点叉 → CloseRequested → kill_backend()（见下）
```

## 关闭流程（优雅，~1–2s）

GUI 进程没有 console，`GenerateConsoleCtrlEvent`（CTRL_BREAK）送不到后端，所以
**主通道改为 HTTP**：

```
关窗 → kill_backend(port):
  1) GET  /api/system/health   取 qai_csrf cookie（双 submit token）
  2) POST /api/system/exit     带 Cookie + X-QAI-CSRF header
       → 后端调度 exit code 0（supervisor 不重启）
       → 跑完 FastAPI lifespan shutdown（close DB / stop daemons）
       → supervisor 干净退出
  3) 等 child 退出（HTTP 已 ack 时通常 1–2s）
  4) 兜底：HTTP 失败时才尝试 CTRL_BREAK，再不行 taskkill /T /F /PID
```

> 历史：早期用 CTRL_BREAK 作主通道，GUI 无 console 导致每次都白等 10s graceful
> timeout 才 taskkill（关窗 ~11s）。改 HTTP /exit 后降到 ~1.3s 且真正优雅。

## 与现有启动器（`Start.bat`）的关系

完全平行。`Start.bat` 启动浏览器版（系统浏览器开 `127.0.0.1:8000`），桌面壳启动
WebView 版（端口随机、避免占用冲突）。两者都通过 `apps.cli.serve` 监管同一份后端，
复用 `_Supervisor` 的 reboot-75 与优雅停止链路（详见 `apps/cli/serve.py`）。

§3.1 已锁契约（路由 / SSE / WS frame / `REBOOT_EXIT_CODE` / CSRF /
OpenAI Compat 三路由）**完全不变**。`/api/system/exit` 是**新增**路由（§3.1 允许新增）。

## 应用图标

图标源自前端 `index.html` 的内联品牌 favicon（紫→蓝渐变 `#7c6cff`→`#60a5fa` 的
NPU 芯片 + 四角节点 + 深色圆角底，与应用内 `.sidebar-logo-glyph` 同一标记），
桌面 exe / 任务栏 / 窗口图标与浏览器标签页图标 1:1 一致。

- `icons/source-icon.svg`：矢量源（与 favicon 一致）
- `icons/source-icon.png`：1024×1024 光栅源（`icons/_render_icon.py` 用 Pillow 按
  SVG 几何 + 渐变重绘生成）
- 其余 `icon.ico` / `icon.icns` / `*.png` / `Square*Logo.png`：同样由
  `icons/_render_icon.py` 一并生成全套（无需 Rust / `cargo tauri` 工具链），
  `tauri.conf.json` 的 `bundle.icon` 引用它们。

更新图标：改 `source-icon.svg` 与 `_render_icon.py` 的几何/配色 → 重跑
`_render_icon.py`（会覆盖 `source-icon.png` 及全套 PNG/ICO/ICNS）。

## 已知限制

1. **venv 必须存在**：壳依次尝试 host_arch 指定的 venv、`.venv_arm64_313`、
   `.venv_x64_313`；全部不存在时回退到 PATH 上的 `python.exe`，多半跑不通；
   后续可加 first-run 引导跳到 Setup。
2. **MSI 安装版的资源定位**：`find_repo_root` 靠源码树标记（`src/`+`apps/`）定位，
   dev-checkout 与 `.build/` 重定向下都正确；但**真实最终用户机（无源码树）** 上会
   找不到，需 D5 用 PyInstaller 把后端冻结成 sidecar（PENDING-WORK ARCH-DESK-2）。
3. **强杀残留**：壳被任务管理器/断电强杀（不走 CloseRequested）时后端进程树成
   orphan，需 Job Object 兜底（PENDING-WORK ARCH-DESK-1）。
4. **未实现 reboot 转发**：后端 exit-code 75 重启时 WebView 不自动 reload。
5. **未实现单实例锁**：双击两次启两份（端口随机不冲突，但浪费资源）。

## 跨平台说明

按 `AGENTS.md` 跨平台前瞻原则，所有 Windows 专用代码（`creation_flags`、
`%LOCALAPPDATA%\...\<venv>` 路径选择、`GenerateConsoleCtrlEvent`）都在
`#[cfg(target_os = "windows")]` 守护下。非 Windows 编译可通过，运行时回退到
`python3`，未做实测——当前不支持。

## 文件清单

| 路径 | 用途 |
|---|---|
| `build.ps1` | **构建入口**：编译 + 自动拷产物到 `dist/desktop/` |
| `src-tauri/.cargo/config.toml` | 把 Cargo target-dir 重定向到 `.build/desktop-target/` |
| `src-tauri/Cargo.toml` | Rust crate 配置 |
| `src-tauri/tauri.conf.json` | Tauri 应用配置（productName / bundle / frontendDist / icon） |
| `src-tauri/build.rs` | `tauri_build::build()` 编译脚本 |
| `src-tauri/src/main.rs` | 入口（Windows 子系统标记 + 调 `lib::run`） |
| `src-tauri/src/lib.rs` | 选端口 / find_repo_root / spawn 后端 / 探活 / 开窗 / HTTP 优雅退出 |
| `src-tauri/icons/source-icon.svg` | 图标矢量源（同前端 favicon） |
| `src-tauri/icons/_render_icon.py` | SVG→1024 PNG 渲染脚本（Pillow） |
| `src-tauri/icons/*` | `tauri icon` 生成的全套图标 |
