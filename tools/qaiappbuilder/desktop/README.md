# QAI ModelBuilder 桌面版(Tauri 2 + WebView2)

一个很小的桌面壳(ARM64 约 2.9 MB,x64 约 3.1 MB),**只负责开一个应用窗口**,前端和后端都没有改:窗口加载的就是后端提供的同一份 `http://localhost:8989/`,和浏览器里看到的一样。安装包的开始菜单和桌面快捷方式指向它;浏览器版仍是安装目录里的 `Start.bat`。

## 它做什么

1. 找到应用根目录(向上查找同时含 `src/`、`apps/`、`factory/config/ports.json` 的目录)。
2. 用 `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_<arch>_313\Scripts\python.exe`(`<arch>` 取自 `data\config\host_arch`,没有就按系统架构)运行 `python -m apps.cli.serve --port 8989`:隐藏控制台窗口,输出写到 `data\logs\desktop-backend.log`。
3. 先显示启动页,轮询 `GET /api/system/health`,返回 200 后把窗口导航到 `http://localhost:8989/chat`。
4. 关窗口时询问"退出 / 最小化到托盘 / 取消";退出时先让后端正常关闭。

端口固定为 `factory/config/ports.json` 的 `backend`(8989):SSO 的回调地址写死了端口,壳不会换端口。该端口已被占用(例如正在运行 `Start.bat`)时**不启动后端**,在窗口里提示原因。

## 前置条件(只有编译的人需要)

| 组件 | 说明 |
|---|---|
| Rust(rustup) | `winget install Rustlang.Rustup`,装完重开终端。`build.ps1` 会自动补装 `aarch64-pc-windows-msvc` 和 `x86_64-pc-windows-msvc` 两个目标 |
| Visual Studio 2022 的 C++ 工具 | 需要 MSVC 的 ARM64 和 x64 两套工具(Build Tools 的"使用 C++ 的桌面开发"工作负载) |
| Windows SDK | 例如 10.0.22621 |
| WebView2 运行时 | 运行时才需要,Windows 10/11 默认自带 |

**不需要** Tauri CLI(`cargo tauri`)、Node 或 pnpm;直接用 `cargo build`。最终用户的机器上不需要任何编译工具。

## 构建

```powershell
# 在 tools\qaiappbuilder 目录下
.\desktop\build.ps1                 # 默认 -Arch both:arm64 和 x64
.\desktop\build.ps1 -Arch arm64     # 只编译一个架构
.\desktop\build.ps1 -SkipTests      # 跳过单元测试
```

产物:`dist\desktop\<arch>\QAIModelBuilder.exe`(`dist\` 已被 `.gitignore`)。脚本会:

- 检查 `cargo`,缺少的 Rust 目标自动 `rustup target add`;
- 在本机架构上运行 `cargo test`(单元测试只在本机架构运行,另一个架构只编译);
- 对每个架构 `cargo build --release`,并校验 exe 的 PE 机器类型(ARM64 = `0xAA64`,x64 = `0x8664`),不符就报错;
- 把 exe 复制到 `dist\desktop\<arch>\`。

编译缓存在 `.build\desktop-target\`(由 `src-tauri\.cargo\config.toml` 指定),所以 `desktop\` 目录只含源码。首次编译需要下载并编译约 450 个 crate:ARM64 约 3–4 分钟,x64 约 1–2 分钟;之后增量编译约 40 秒。公司代理常拦截证书吊销检查,`build.ps1` 默认设置了 `CARGO_HTTP_CHECK_REVOKE=false`。

### 打进安装包

安装包会带上对应架构的 exe,**所以必须先编译、再打包**:

```powershell
.\desktop\build.ps1 -Arch both      # 1. 编译桌面版
.\installer\build_installer.ps1     # 2. 打安装包(详见 installer\README.md)
```

`build_installer.ps1` 发现 `dist\desktop\<arch>\QAIModelBuilder.exe` 不存在会直接报错并提示先运行 `build.ps1`。

## 运行行为

### 关窗口

弹出系统对话框(`TaskDialogIndirect`,文字按 Windows 显示语言选中文或英文):

- **退出程序**:停止后台服务并关闭窗口。
- **最小化到托盘**:隐藏窗口,服务继续运行,从托盘恢复。
- **取消**(Esc 或标题栏的 X):什么都不做。
- 勾选"记住我的选择"后,结果保存在 `data\config\desktop-settings.json` 的 `close_action`(`ask` / `quit` / `tray`)。**删掉该文件(或把值改回 `ask`)就会重新询问**。
- 后端还没就绪或已出错(窗口显示的是启动页/错误页)时,关窗口直接退出,不询问。

### 托盘与单实例

托盘图标:左键恢复窗口,右键菜单"显示窗口 / 退出"。单实例:再次双击快捷方式不会再启动一份后端,只会把已有窗口调到前台(包括从托盘隐藏的状态)。托盘图标可能在任务栏的"隐藏图标"区里。

### 退出与异常

- 正常退出:先 `POST /api/system/exit`(后端要求 CSRF 双提交:先 `GET /api/system/health` 取 `qai_csrf` cookie,再带同值的 `X-QAI-CSRF` 头),等后端的 supervisor 自行退出(最多 15 秒);超时才 `taskkill /T /F` 整个进程树。
- 壳被任务管理器强杀:后端放在带 `KILL_ON_JOB_CLOSE` 的 Job Object 里,系统会一起回收,不会残留 Python 进程。
- 后端中途崩溃或被外部杀掉:窗口回到错误页,显示退出码和日志路径,并把窗口调到前台。
- 找不到安装目录或 venv:窗口里直接提示怎么修复(重新安装,或运行 `setup_env.bat` / "修复"快捷方式)。

### 链接与弹窗

- 应用自己的地址(`localhost` / `127.0.0.1` + 后端端口)留在窗口里,SSO 登录弹窗也一样。
- 其他 `http(s)` 链接交给系统默认浏览器(`ShellExecuteW`)。
- 其他协议(`file:`、`javascript:` 等)一律拒绝。
- `<a download>` 下载、剪贴板、麦克风接口在 WebView2 里可用(已实测)。

## 与 `Start.bat` 的关系

互相独立,共用同一个后端入口(`apps.cli.serve`)和同一个 venv。两者**不能同时运行**,因为都要用 8989 端口:先开的占着,后开的会提示端口被占用。浏览器版现在只有安装目录里的 `Start.bat`,没有开始菜单/桌面快捷方式。

## 源码结构

| 路径 | 作用 |
|---|---|
| `build.ps1` | 构建入口(上面的流程) |
| `splash/index.html` | 启动页/错误页,自带样式,跟随系统深浅色 |
| `src-tauri/Cargo.toml`、`Cargo.lock` | 依赖;`Cargo.lock` 提交以保证可重复构建 |
| `src-tauri/tauri.conf.json` | 应用配置(`frontendDist` 指向 `splash/`,不预置窗口) |
| `src-tauri/src/main.rs` | 入口,Windows 子系统(无控制台) |
| `src-tauri/src/lib.rs` | 启动流程、窗口、托盘、关窗询问、链接拦截、退出、后端监视 |
| `src-tauri/src/backend.rs` | 启动后端(Job Object)、健康检查、优雅停止、强杀进程树 |
| `src-tauri/src/layout.rs` | 定位应用根目录、venv python、端口、`host_arch` |
| `src-tauri/src/close_dialog.rs` | 关窗口三选一对话框 |
| `src-tauri/src/settings.rs` | 读写 `desktop-settings.json` |
| `src-tauri/src/i18n.rs` | 中英文文案 |
| `src-tauri/icons/` | 图标;`render_icon.py` 按前端 `index.html` 里的 favicon 几何重绘(需要 Pillow) |
| `src-tauri/gen/` | `tauri-build` 生成的文件,被 `desktop\.gitignore` 忽略 |

更新图标:改 `src-tauri/icons/render_icon.py`,运行 `python desktop\src-tauri\icons\render_icon.py`,会重写 `icon.ico` 和 `icon.png`。

## 调试

- 后端输出:`data\logs\desktop-backend.log`。
- 打开 WebView2 调试端口:启动前设置 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333`,然后访问 `http://127.0.0.1:9333/json`。
- 单元测试:`cd desktop\src-tauri; cargo test --release --lib`(3 项:`host_arch` 解析、端口缺省、设置读写)。

## 已知限制

- 没有"开机自启"。
- 没有自定义标题栏,也没有托盘里的"新建会话 / 切换模型"这类与前端联动的功能(需要改前端)。
- 壳依赖后端的 `/api/system/health` 和 `/api/system/exit`。
- 只支持 Windows(`#[cfg(windows)]` 之外的路径没有实现)。
- 实测范围见 `installer\README.md` 的"桌面版窗口 → 实测情况"。
