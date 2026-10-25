# QAI ModelBuilder 安装包

用 Inno Setup 为每个架构各打一个 `.exe`:预编译的前端 + 桌面版窗口(`QAIModelBuilder.exe`)+ 后端源码 + `uv` + `aria2c`。
安装时再联网建 Python 环境。**不含 QAIRT SDK、不含测试/文档/模型。**

| 产物 | 说明 |
|---|---|
| `QAIModelBuilder-<ver>-x64-setup.exe` | x64 / Windows-on-ARM 上跑 x64 都可用 |
| `QAIModelBuilder-<ver>-arm64-setup.exe` | 仅 ARM64 |

## 准备构建环境

只有**打包的人**需要这些;安装包的最终用户不需要任何一项。下面的命令都在 PowerShell 里执行,装完每一项后**重新打开终端**(让 PATH 生效),最后用"自检"一节确认。

| 组件 | 用途 | 什么时候可以不装 |
|---|---|---|
| Node.js + pnpm | `Build.bat` 构建前端 | 已有 `frontend\dist`,打包时加 `-SkipFrontend` |
| Rust + VS 2022 C++ 工具 + Windows SDK | 编译桌面版窗口 | 已有 `dist\desktop\<arch>\QAIModelBuilder.exe`,打包时加 `-SkipDesktop` |
| Inno Setup 6 | 生成安装包 | **必装** |
| 能访问 pypi.org 的网络 | 取目标架构的 `uv.exe` | **必需** |

### 1. Node.js 和 pnpm

要求(来自 `frontend\package.json`):Node ≥ 22,pnpm ≥ 9,仓库固定用 `pnpm@11.9.0`。

```powershell
winget install OpenJS.NodeJS          # 装 Node.js(自带 npm)
# 重新打开终端后:
npm install -g pnpm@11.9.0            # 装与仓库一致的 pnpm 版本
```

`Build.bat` 找 Node/pnpm 的顺序:先看 `%LOCALAPPDATA%\QAIModelBuilder\node\`(`Setup.bat --frontend` 装的便携版),找不到就用 PATH 上的。**所以上面两条命令装的系统版本就够用**,不需要跑 `Setup.bat --frontend`。

> 用 `npm install -g pnpm`,不要用 `corepack enable`:较新的 Node 不再自带 corepack。

### 2. Rust

```powershell
winget install Rustlang.Rustup
# 重新打开终端后,可提前把两个架构的目标都装上(不装也行,编译时会自动补装):
rustup target add aarch64-pc-windows-msvc x86_64-pc-windows-msvc    # 可选,提前装好
```

`desktop\build.ps1` 会自己检查 `cargo` 和两个目标,缺目标时自动 `rustup target add`;缺 `cargo` 时直接报错并提示上面的 winget 命令。

### 3. Visual Studio 2022 的 C++ 工具和 Windows SDK

Rust 的 MSVC 目标需要链接器。**需要同时有 ARM64 和 x64 两套工具**,因为要给两个架构各编译一个 exe。已经装了完整的 Visual Studio 2022 的话,用 Visual Studio Installer 勾上下面三项即可;没有的话装 Build Tools:

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools --override "--passive --wait --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621"
```

三个组件的 ID:

| 组件 ID | 说明 |
|---|---|
| `Microsoft.VisualStudio.Component.VC.Tools.x86.x64` | MSVC x64 |
| `Microsoft.VisualStudio.Component.VC.Tools.ARM64` | MSVC ARM64 |
| `Microsoft.VisualStudio.Component.Windows11SDK.22621` | Windows SDK |

> 这套 ID 和 `scripts\setup\install_vs.ps1` 用的一致。安装体积较大(几个 GB),需要管理员权限。也可以用 Visual Studio Installer 的图形界面,在“使用 C++ 的桌面开发”里勾选上面三项。

### 4. Inno Setup 6

```powershell
winget install JRSoftware.InnoSetup --scope user
```

`build_installer.ps1` 会在 `%LOCALAPPDATA%\Programs\Inno Setup 6\`、`Program Files (x86)\Inno Setup 6\`、`Program Files\Inno Setup 6\` 和 PATH 里找 `ISCC.exe`;装在别处时用 `-IsccPath <路径>` 指定。

### 自检

```powershell
node --version          # 应 >= v22
pnpm --version          # 应为 11.9.0(>= 9 也可构建)
cargo --version
rustup target list --installed          # 应含 aarch64-pc-windows-msvc 和 x86_64-pc-windows-msvc
Test-Path "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"    # True
```

都通过后,运行下面"打包"一节的命令。

## 打包

**一条命令完成**(在 `tools\qaiappbuilder` 目录下):

```powershell
.\installer\build_installer.ps1
```

它依次做:① 跑 `Build.bat` 刷新前端 → ② 调 `desktop\build.ps1` 编译桌面版 exe(带 Rust 单元测试和架构校验) → ③ 按白名单整理每个架构的文件 → ④ 调 Inno Setup 出 x64 + arm64 两个安装包。任何一步失败都会立刻停下并报错,不会继续打出一个缺 exe 的安装包。

常用参数:

```powershell
.\installer\build_installer.ps1 -Arch x64                # 只出 x64(桌面版也只编译 x64)
.\installer\build_installer.ps1 -SkipFrontend            # 沿用现有 frontend\dist,不跑 Build.bat
.\installer\build_installer.ps1 -SkipDesktop             # 不编译桌面版,直接用 dist\desktop\<arch>\ 里现有的 exe
.\installer\build_installer.ps1 -KeepStage               # 保留 .build\installer\stage_<arch>\ 便于检查
.\installer\build_installer.ps1 -Version 3.1.1           # 覆盖版本号(默认取 build_info.json 的 version)
.\installer\build_installer.ps1 -Help                      # 显示用法后退出(-h 也可以)
```

`-SkipDesktop` 用在**没有 Rust / VS C++ 工具的机器**上:把别处编好的 `dist\desktop\<arch>\QAIModelBuilder.exe` 拷过来,再加这个参数。对应的 exe 不存在时,在开始整理文件之前就报错。也可以单独编译桌面版:`.\desktop\build.ps1 -Arch both`(细节见 [`desktop/README.md`](../desktop/README.md))。

前置条件见上面的“准备构建环境”。

输出在 `dist\installer\`(已被 `.gitignore`):`QAIModelBuilder-<ver>-x64-setup.exe`、`QAIModelBuilder-<ver>-arm64-setup.exe`。

## 包里有什么(白名单)

`src` `apps` `factory` `scripts` `interfaces` `skills` `frontend\dist` `pyproject.toml` `build_info.json`
`desktop\QAIModelBuilder.exe`(桌面版窗口,见下)、`setup_env.bat` `Start.bat` `qai.bat` `Console.bat` `Uninstall.bat` `clean_env.bat` 及 README,
外加 `data\bin\uv\uv.exe`(**按目标架构**从 PyPI 取)、`data\bin\aria2c\aria2c.exe`,以及 **FileGuard 的 `vendor\bin\<arch>\guard64.dll`**(ARM64 另有 `guard-injector.exe`)和标记文件 `vendor\bin\guard.source`。

guard 文件取自发布页的 `guard.zip`,**只放本架构的那几个**(x64 约 0.7 MB、ARM64 约 0.7 MB;整个 zip 有 30 MB,因为里面还带两个架构的 ast-grep)。`guard.source` 写的是 `setup_env.bat` 自己会下载的那个 URL,脚本看到标记与 URL 一致且 dll 在,就跳过下载,所以**安装时不再联网下载 guard.zip**。来源顺序:`-GuardZip` 参数、`Downloads\guard.zip`、发布页 URL;都拿不到时只打印一行提示,安装时仍由 `setup_env.bat` 下载。发布标签变了(`setup_env.bat` 里的 `QAI_AB_RELEASE_TAG`),重新打包即可;旧包里的标记和新标签不一致时,安装会重新下载并替换。

还有 **`vendor\tiktoken\`**(约 5.3 MB,压缩后约 1.5 MB):`cl100k_base` 和 `o200k_base` 两个 BPE 词表,程序用它离线估算 token 数。词表不在 PyPI 的 tiktoken wheel 里,不打进包的话运行时会去 `openaipublic.blob.core.windows.net` 下载,下不到就退回按长度粗估。打包时的来源顺序:`.build\installer\_dl\tiktoken` 缓存、官方 URL、`Downloads\vendor-deps.7z`(用 `tar.exe` 只解出这两个文件);下载内容会校验大小和首行格式。每个词表写两份文件名(URL 的 SHA1 是 tiktoken 实际读取的名字,SHA256 是 `download_bpe_files.py` 文档里的名字,内容相同)。两个词表任一拿不到就整体不放,安装后行为同没有这个目录。

排除:`tests`、`__pycache__`、`*.pyc`、`node_modules`、`docs`、`samples`、`models`、`.qai`、`desktop`(源码;编译好的 exe 已单独打入)、`vendor`(除上面的 guard 文件和 tiktoken 词表)、`data`(用户数据)。
新增运行时依赖的目录时,要把它加进 `build_installer.ps1` 的 `$Dirs`。

## 安装时发生什么

1. 向导里选的是**上层目录**(默认 `%LOCALAPPDATA%\QAIModelBuilder`,免管理员,可改),程序固定装到它下面的 `app\` 子目录。
   `app\` 里只放本程序的东西(安装的文件 + 运行时生成的 `data\`、`models\`、`vendor\` 等),卸载程序 `unins000.exe` 也在里面。
   - 所选目录下的 `app\` 已有不是本程序装的文件:拒绝安装,提示换目录。
   - 所选目录是旧版本的安装目录(旧版直接装在 `...\QAIModelBuilder\app` 里,没有再嵌套一层):拒绝安装,提示先卸载旧版。新旧目录结构不兼容。
   - `app\` 里只剩上次卸载保留的 `data\`:允许覆盖安装,数据原样保留。
2. 向导里可选:镜像(自动 / 官方 / 国内)、HTTP 代理。
3. 执行 `setup_env.bat --arch <arch> --no-pause --with-browser --with-astgrep [--mirror ..] [--proxy ..]`(**不带 `--builder`**;两个 `--with-*` 写在 `.iss` 的 `#define SetupComponents` 里,安装和"修复"快捷方式共用):
   建 Python 3.13 运行时 venv(`%LOCALAPPDATA%\QAIModelBuilder\envs\`)、装依赖、初始化 `data\`。
   ARM64 另外会在线下载 `vendor-deps.7z`(约 210 MB,来自 aidevhome.com)。
   - `--with-browser`:`search` extra(`playwright==1.61.0`、`selectolax`、`lxml`、`cssselect`、`browserforge`,来自 PyPI)+ `python -m playwright install chromium`。
     Chromium 来自 `cdn.playwright.dev`(国内档由 `net_config.ps1` 设 `PLAYWRIGHT_DOWNLOAD_HOST` 指向 npmmirror),装到 `%LOCALAPPDATA%\ms-playwright\`。失败不中断安装,只是浏览器工具不可用。
   - `--with-astgrep`:从 PyPI 的 `ast-grep-cli` 0.45.1 wheel 取出 `ast-grep.exe`、`sg.exe`(各约 49 MB)到 `vendor\bin\<arch>\`;PyPI 不通时回退 GitHub release。ARM64 上 `vendor-deps.7z` 已带这两个文件,会直接 `[SKIP]`。
   **不装**模型转换环境(`.venv_x64_310`)和 QAIRT SDK。
4. 环境配置失败时,文件仍已安装;开始菜单有"修复 / 完成环境配置",重跑 `setup_env.bat`,可断点续。
5. 成功后可勾选"立即启动",打开**桌面版**窗口(`desktop\QAIModelBuilder.exe`)。开始菜单和桌面快捷方式也指向它;浏览器版仍然是安装目录里的 `Start.bat`(不再有快捷方式)。

## QAIRT(模型转换)

安装包**不装**模型转换环境:不含 QAIRT SDK,也不含 `.venv_x64_310`。
需要时在应用目录手动运行 `setup_env.bat --arch <arch> --builder`,它会依次建 310 venv、装转换器依赖、下载 SDK、生成 `data\config\qairt_env.json`。
**后台自动安装(310 venv + SDK 一起)尚未实现,是下一步**:需要在后端新增安装任务、进度接口和前端入口。

## 卸载

控制面板 / 开始菜单卸载会先结束从 `app\` 和 `envs\` 启动的进程(后台服务、转换、下载),然后弹出一个选项窗口(大小、位置与"卸载状态"窗口一致,单独一个窗口,不在状态窗口里:Inno 在卸载期间会禁用状态窗口,放进去的按钮点不了),列出可选删除的内容,**默认全部勾选**(取消勾选即保留;点"全部保留"或关闭窗口则都不删):

- **Python 环境**(`envs\`):目录存在才列出。
- **你的数据**(`app\data`:聊天记录、设置、密钥):目录存在才列出。
- **浏览器工具用的 Chromium**(`%LOCALAPPDATA%\ms-playwright`):目录存在才列出。这是 Playwright 的按用户共享缓存,其他用 Playwright 的程序也可能在用,所以和其它项一样可以取消勾选。
- **QAIRT SDK `<版本>`**:`data\config\qairt_env.json` 的 `qairt_sdk_root` 指向的目录存在就列出(不再检查标记文件;默认位置 `C:\Qualcomm\AIStack\QAIRT\<版本>`,在 `app\` 之外)。路径必须是绝对本地路径且至少 3 层深,避免误删盘根或 `C:\Qualcomm`。勾选项旁边显示完整路径。
- 程序文件一定会删。
- 没勾"你的数据"时:删除 `app\` 里除 `data\` 以外的全部内容(包括运行时下载的 `models\` 等)。勾了:删除整个 `app\`;所选的上层目录空了也一起删除。`data\downloads\_qairt_tmp.zip` 这类下载临时文件无论如何都会删。
- 静默卸载(`/SILENT`、`/VERYSILENT`)不弹窗,什么可选内容都不删(与"全部保留"相同)。
- uv / pip 的全局缓存(`%LOCALAPPDATA%\uv\cache` 等)是机器上共享的,不删。

## 已知限制

- 安装时 `setup_env.bat` 的完整输出在 `<安装目录>\data\logs\install_setup_env.log`,排错先看它。
- 安装路径含空格或非 ASCII 会给警告(部分脚本拼路径,未全面验证)。
- Inno Setup 6.7 默认开启 RedirectionGuard,子进程继承后 `uv python install` 建 junction 会报 `os error 448`,所以 `.iss` 里设了 `RedirectionGuard=no`。
- 打包白名单要包含 `tools/`(`scripts.init.install` 需要它)。
- x64 的 `qai_appbuilder` wheel 只在 GitHub release 上,无法访问 GitHub 时会装不上(`setup_env.bat` 会给 `[ATTENTION]`)。
- `ChineseSimplified.isl` 是社区翻译(kira-96/Inno-Setup-Chinese-Simplified-Translation),Inno 官方不再自带。

## 桌面版窗口(`desktop\QAIModelBuilder.exe`)

一个 Tauri 2 + WebView2 的小壳(各架构分别编译、分别打入对应安装包),开始菜单和桌面快捷方式都指向它;浏览器版仍是安装目录里的 `Start.bat`(不再有快捷方式)。**前端和后端都没改**:窗口加载的就是后端提供的同一份 `http://localhost:8989/`。

- 启动:先显示启动页,后台用安装时建的 venv 运行 `python -m apps.cli.serve --port 8989`(隐藏控制台窗口),`/api/system/health` 返回 200 后再打开应用页。端口从 `factory/config/ports.json` 读,固定为 8989(SSO 回调地址写死了端口)。端口被占用(例如正在跑 `Start.bat`)时不启动后端,直接在窗口里提示。
- 关窗口:弹出"退出 / 最小化到托盘 / 取消",可勾选"记住我的选择"。记住的结果存在 `data\config\desktop-settings.json` 的 `close_action`(`ask` / `quit` / `tray`),删掉这个文件就会重新询问。后端还没起来或已出错时关窗直接退出。
- 托盘:左键恢复窗口,右键菜单"显示窗口 / 退出"。单实例:再次双击快捷方式只会把已有窗口调到前台。
- 退出:先 `POST /api/system/exit`(带 CSRF 令牌)让后端正常关闭,超时才强杀进程树。后端放在 Job Object 里,壳被任务管理器强杀时,后端也会被系统回收。后端中途崩溃或被杀时,窗口回到错误页并显示日志路径(`data\logs\desktop-backend.log`)。
- 链接:应用自己的地址在窗口里打开(SSO 登录弹窗也一样);其他 http(s) 链接交给系统浏览器;其他协议一律拒绝。
- 语言:按 Windows 显示语言选中文或英文。
- 构建、源码结构、调试方法:见 [`desktop/README.md`](../desktop/README.md)。打包命令见上面的"打包"一节(一条命令会自动编译桌面版)。
