# QAI ModelBuilder 安装包

用 Inno Setup 为每个架构各打一个 `.exe`:预编译的前端 + 桌面版窗口(`QAIModelBuilder.exe`)+ 后端源码 + `uv` + `aria2c`。
安装时再联网建 Python 环境。**不含 QAIRT SDK、不含测试/文档/模型。**

| 产物 | 大小(实测 x64) | 说明 |
|---|---|---|
| `QAIModelBuilder-<ver>-x64-setup.exe` | 约 20 MB | x64 / Windows-on-ARM 上跑 x64 都可用 |
| `QAIModelBuilder-<ver>-arm64-setup.exe` | 见 build 输出 | 仅 ARM64 |

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
```

`-SkipDesktop` 用在**没有 Rust / VS C++ 工具的机器**上:把别处编好的 `dist\desktop\<arch>\QAIModelBuilder.exe` 拷过来,再加这个参数。对应的 exe 不存在时,在开始整理文件之前就报错。也可以单独编译桌面版:`.\desktop\build.ps1 -Arch both`(细节见 [`desktop/README.md`](../desktop/README.md))。

前置条件:

| 步骤 | 需要 |
|---|---|
| 编译桌面版(默认会做) | Rust、VS 2022 的 C++ 工具(ARM64 + x64)、Windows SDK。安装和细节见 [`desktop/README.md`](../desktop/README.md) |
| 打安装包 | Inno Setup 6(`winget install JRSoftware.InnoSetup --scope user`),本机能访问 pypi.org(取目标架构的 `uv.exe`) |
| 刷新前端(可用 `-SkipFrontend` 跳过) | Node + pnpm(`Build.bat` 用) |

输出在 `dist\installer\`(已被 `.gitignore`):`QAIModelBuilder-<ver>-x64-setup.exe`、`QAIModelBuilder-<ver>-arm64-setup.exe`。首次完整跑(含编译两个架构的 Rust 依赖)约 4 分钟;之后增量编译和 `-SkipFrontend` 会快很多。

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
   实测 venv 约 0.75 GB,约下载 1 GB,10–20 分钟。
   ARM64 另外会在线下载 `vendor-deps.7z`(约 210 MB,来自 aidevhome.com)。
   - `--with-browser`:`search` extra(`playwright==1.61.0`、`selectolax`、`lxml`、`cssselect`、`browserforge`,来自 PyPI)+ `python -m playwright install chromium`。
     Chromium 来自 `cdn.playwright.dev`(国内档由 `net_config.ps1` 设 `PLAYWRIGHT_DOWNLOAD_HOST` 指向 npmmirror),装到 `%LOCALAPPDATA%\ms-playwright\`。
     实测下载 Chromium + headless shell + ffmpeg + winldd,占盘 **688 MB**,约 50 秒。失败不中断安装,只是浏览器工具不可用。
   - `--with-astgrep`:从 PyPI 的 `ast-grep-cli` 0.45.1 wheel 取出 `ast-grep.exe`、`sg.exe`(各约 49 MB)到 `vendor\bin\<arch>\`;PyPI 不通时回退 GitHub release。ARM64 上 `vendor-deps.7z` 已带这两个文件,会直接 `[SKIP]`。
   **不装**模型转换环境(`.venv_x64_310`,实测约 2.4 GB)和 QAIRT SDK。
4. 环境配置失败时,文件仍已安装;开始菜单有"修复 / 完成环境配置",重跑 `setup_env.bat`,可断点续。
5. 成功后可勾选"立即启动",打开**桌面版**窗口(`desktop\QAIModelBuilder.exe`)。开始菜单和桌面快捷方式也指向它;浏览器版仍然是安装目录里的 `Start.bat`(不再有快捷方式)。

## QAIRT(模型转换)

安装包**不装**模型转换环境:不含 QAIRT SDK(约 1.7–2 GB),也不含 `.venv_x64_310`(约 2.4 GB)。
需要时在应用目录手动运行 `setup_env.bat --arch <arch> --builder`,它会依次建 310 venv、装转换器依赖、下载 SDK、生成 `data\config\qairt_env.json`。
**后台自动安装(310 venv + SDK 一起)尚未实现,是下一步**:需要在后端新增安装任务、进度接口和前端入口。

## 卸载

控制面板 / 开始菜单卸载会先结束从 `app\` 和 `envs\` 启动的进程(后台服务、转换、下载),然后弹出一个选项窗口(大小、位置与"卸载状态"窗口一致,单独一个窗口,不在状态窗口里:Inno 在卸载期间会禁用状态窗口,放进去的按钮点不了),列出可选删除的内容,**默认全部勾选**(取消勾选即保留;点"全部保留"或关闭窗口则都不删):

- **Python 环境**(`envs\`):目录存在才列出。
- **你的数据**(`app\data`:聊天记录、设置、密钥):目录存在才列出。
- **浏览器工具用的 Chromium**(`%LOCALAPPDATA%\ms-playwright`,实测约 0.7 GB):目录存在才列出。这是 Playwright 的按用户共享缓存,其他用 Playwright 的程序也可能在用,所以和其它项一样可以取消勾选。
- **QAIRT SDK `<版本>`**:`data\config\qairt_env.json` 的 `qairt_sdk_root` 指向的目录存在就列出(不再检查标记文件;默认位置 `C:\Qualcomm\AIStack\QAIRT\<版本>`,在 `app\` 之外)。路径必须是绝对本地路径且至少 3 层深,避免误删盘根或 `C:\Qualcomm`。勾选项旁边显示完整路径。
- 程序文件一定会删。
- 没勾"你的数据"时:删除 `app\` 里除 `data\` 以外的全部内容(包括运行时下载的 `models\` 等)。勾了:删除整个 `app\`;所选的上层目录空了也一起删除。`data\downloads\_qairt_tmp.zip` 这类下载临时文件无论如何都会删。
- 静默卸载(`/SILENT`、`/VERYSILENT`)不弹窗,什么可选内容都不删(与"全部保留"相同)。
- uv / pip 的全局缓存(`%LOCALAPPDATA%\uv\cache` 等)是机器上共享的,不删。

## 已知限制 / 未验证

- **已实测(x64 包,在 Windows-on-ARM 上模拟运行,隔离环境)**:静默安装 → `setup_env.bat` 7 步 2m38s 无错误 → `Start.bat` 9s 就绪,`/api/system/health` 200,前端 200 → 静默卸载。(这是加入 `--with-browser --with-astgrep` **之前**的结果。)
- **已实测(加入两个组件后,用打包 stage 目录、隔离的 `LOCALAPPDATA` 直接跑 `setup_env.bat`,未经 Inno 向导)**:
  - ARM64:7 步全部成功,1m31s;Chromium 一步 51s。删掉 `ast-grep.exe` 后重跑,从 PyPI wheel 取回(10.5s),`ast-grep --version` 为 0.45.1。
    `import playwright, selectolax, lxml, browserforge` 通过,HTML 解析后端为 `selectolax`;无头 Chromium 149 能启动并执行页面 JS。
  - x64:前 5 步成功(ast-grep 12.5s,`uv pip` 22.6s,Chromium 52.8s);之后从 GitHub 下 `qai_appbuilder` wheel 时本机到 github.com 的速度只有每秒几 KB,25 分钟后被我中止,所以 x64 的 Step 6–7 本轮没有跑到(和新组件无关)。
- **未测**:arm64 包(原生)、有界面的向导(含新的进度条权重)、卸载窗口里新增的 Chromium 勾选项、真正干净的机器(没有系统 Python / VC++ 运行库)、中文界面实际显示、「修复」快捷方式、代理/国内镜像页(含 Chromium 走 npmmirror)。
- **踩过的坑**:Inno 6.7 默认开 RedirectionGuard,子进程继承后 `uv python install` 建 junction 报 `os error 448`;已在 `.iss` 设 `RedirectionGuard=no`。打包白名单也曾漏掉 `tools/`(`scripts.init.install` 需要它)。
- 安装时 `setup_env.bat` 的完整输出在 `<安装目录>\data\logs\install_setup_env.log`,排错先看它。
- 安装路径含空格或非 ASCII 会给警告(部分脚本拼路径,未全面验证)。
- x64 的 `qai_appbuilder` wheel 只在 GitHub release 上,无法访问 GitHub 时会装不上(`setup_env.bat` 会给 `[ATTENTION]`)。
- `ChineseSimplified.isl` 是社区翻译(kira-96/Inno-Setup-Chinese-Simplified-Translation),Inno 官方不再自带。

## 桌面版窗口(`desktop\QAIModelBuilder.exe`)

一个 Tauri 2 + WebView2 的小壳(ARM64 约 2.9 MB,x64 约 3.1 MB;各架构分别编译、分别打入对应安装包),开始菜单和桌面快捷方式都指向它;浏览器版仍是安装目录里的 `Start.bat`(不再有快捷方式)。**前端和后端都没改**:窗口加载的就是后端提供的同一份 `http://localhost:8989/`。

- 启动:先显示启动页,后台用安装时建的 venv 运行 `python -m apps.cli.serve --port 8989`(隐藏控制台窗口),`/api/system/health` 返回 200 后再打开应用页。端口从 `factory/config/ports.json` 读,固定为 8989(SSO 回调地址写死了端口)。端口被占用(例如正在跑 `Start.bat`)时不启动后端,直接在窗口里提示。
- 关窗口:弹出"退出 / 最小化到托盘 / 取消",可勾选"记住我的选择"。记住的结果存在 `data\config\desktop-settings.json` 的 `close_action`(`ask` / `quit` / `tray`),删掉这个文件就会重新询问。后端还没起来或已出错时关窗直接退出。
- 托盘:左键恢复窗口,右键菜单"显示窗口 / 退出"。单实例:再次双击快捷方式只会把已有窗口调到前台。
- 退出:先 `POST /api/system/exit`(带 CSRF 令牌)让后端正常关闭,超时才强杀进程树。后端放在 Job Object 里,壳被任务管理器强杀时,后端也会被系统回收。后端中途崩溃或被杀时,窗口回到错误页并显示日志路径(`data\logs\desktop-backend.log`)。
- 链接:应用自己的地址在窗口里打开(SSO 登录弹窗也一样);其他 http(s) 链接交给系统浏览器;其他协议一律拒绝。
- 语言:按 Windows 显示语言选中文或英文。
- 构建、源码结构、调试方法:见 [`desktop/README.md`](../desktop/README.md)。打包命令见上面的"打包"一节(一条命令会自动编译桌面版)。

### 实测情况(隔离环境,ARM64 原生)

- 通过:静默安装 → 开始菜单/桌面快捷方式都指向 exe → 14 s 后端就绪、窗口加载真实前端;关窗对话框、勾选"记住"后下次直接退出 / 直接最小化、再次启动把隐藏的窗口拉回、退出后后端进程为 0、端口被占用、强杀壳后后端被回收、后端被杀→错误页、`window.open` 外链走系统浏览器、`<a download>` 下载、剪贴板 / 麦克风 API 可用、托盘图标存在。
- **未测**:x64 成品在 x64 机器上运行(只编译并校验了 PE 架构);真实 SSO 登录流程;真实麦克风权限弹窗;卡在 Setup 进度中关窗;卸载窗口(测试安装目录被我手动删除,卸载器没跑);开机自启没有实现。
