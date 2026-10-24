# QAI ModelBuilder 安装包(浏览器版)

用 Inno Setup 为每个架构各打一个 `.exe`:预编译的前端 + 后端源码 + `uv` + `aria2c`。
安装时再联网建 Python 环境。**不含 QAIRT SDK、不含桌面壳(Tauri)、不含测试/文档/模型。**

| 产物 | 大小(实测 x64) | 说明 |
|---|---|---|
| `QAIModelBuilder-<ver>-x64-setup.exe` | 约 20 MB | x64 / Windows-on-ARM 上跑 x64 都可用 |
| `QAIModelBuilder-<ver>-arm64-setup.exe` | 见 build 输出 | 仅 ARM64 |

## 打包

前置:Inno Setup 6(`winget install JRSoftware.InnoSetup --scope user`),本机能访问 pypi.org(取目标架构的 `uv.exe`)。

```powershell
# 在仓库根执行
.\installer\build_installer.ps1                  # 先跑 Build.bat 刷新前端,再出 x64 + arm64
.\installer\build_installer.ps1 -Arch x64 -SkipFrontend   # 只出 x64,沿用现有 frontend\dist
```

输出在 `dist\installer\`(已被 `.gitignore`)。`-KeepStage` 保留 `.build\installer\stage_<arch>\` 便于检查。
版本号取自 `build_info.json` 的 `version`,可用 `-Version` 覆盖。

## 包里有什么(白名单)

`src` `apps` `factory` `scripts` `interfaces` `skills` `frontend\dist` `pyproject.toml` `build_info.json`
`setup_env.bat` `Start.bat` `qai.bat` `Console.bat` `Uninstall.bat` `clean_env.bat` 及 README,
外加 `data\bin\uv\uv.exe`(**按目标架构**从 PyPI 取)和 `data\bin\aria2c\aria2c.exe`。

排除:`tests`、`__pycache__`、`*.pyc`、`node_modules`、`docs`、`samples`、`models`、`.qai`、`desktop`、`vendor`、`data`(用户数据)。
新增运行时依赖的目录时,要把它加进 `build_installer.ps1` 的 `$Dirs`。

## 安装时发生什么

1. 向导里选的是**上层目录**(默认 `%LOCALAPPDATA%\QAIModelBuilder`,免管理员,可改),程序固定装到它下面的 `app\` 子目录。
   `app\` 里只放本程序的东西(安装的文件 + 运行时生成的 `data\`、`models\`、`vendor\` 等),卸载程序 `unins000.exe` 也在里面。
   - 所选目录下的 `app\` 已有不是本程序装的文件:拒绝安装,提示换目录。
   - 所选目录是旧版本的安装目录(旧版直接装在 `...\QAIModelBuilder\app` 里,没有再嵌套一层):拒绝安装,提示先卸载旧版。新旧目录结构不兼容。
   - `app\` 里只剩上次卸载保留的 `data\`:允许覆盖安装,数据原样保留。
2. 向导里可选:镜像(自动 / 官方 / 国内)、HTTP 代理。
3. 执行 `setup_env.bat --arch <arch> --no-pause [--mirror ..] [--proxy ..]`(**不带 `--builder`**):
   建 Python 3.13 运行时 venv(`%LOCALAPPDATA%\QAIModelBuilder\envs\`)、装依赖、初始化 `data\`。
   实测 venv 约 0.75 GB,约下载 1 GB,10–20 分钟。
   ARM64 另外会在线下载 `vendor-deps.7z`(约 210 MB,来自 aidevhome.com)。
   **不装**模型转换环境(`.venv_x64_310`,实测约 2.4 GB)和 QAIRT SDK。
4. 环境配置失败时,文件仍已安装;开始菜单有"修复 / 完成环境配置",重跑 `setup_env.bat`,可断点续。
5. 成功后可勾选"立即启动",即运行 `Start.bat`(自动开浏览器)。

## QAIRT(模型转换)

安装包**不装**模型转换环境:不含 QAIRT SDK(约 1.7–2 GB),也不含 `.venv_x64_310`(约 2.4 GB)。
需要时在应用目录手动运行 `setup_env.bat --arch <arch> --builder`,它会依次建 310 venv、装转换器依赖、下载 SDK、生成 `data\config\qairt_env.json`。
**后台自动安装(310 venv + SDK 一起)尚未实现,是下一步**:需要在后端新增安装任务、进度接口和前端入口。

## 卸载

控制面板 / 开始菜单卸载会先结束从 `app\` 和 `envs\` 启动的进程(后台服务、转换、下载),然后弹出一个选项窗口(大小、位置与"卸载状态"窗口一致,单独一个窗口,不在状态窗口里:Inno 在卸载期间会禁用状态窗口,放进去的按钮点不了),列出可选删除的内容,**默认全部勾选**(取消勾选即保留;点"全部保留"或关闭窗口则都不删):

- **Python 环境**(`envs\`):目录存在才列出。
- **你的数据**(`app\data`:聊天记录、设置、密钥):目录存在才列出。
- **QAIRT SDK `<版本>`**:`data\config\qairt_env.json` 的 `qairt_sdk_root` 指向的目录存在就列出(不再检查标记文件;默认位置 `C:\Qualcomm\AIStack\QAIRT\<版本>`,在 `app\` 之外)。路径必须是绝对本地路径且至少 3 层深,避免误删盘根或 `C:\Qualcomm`。勾选项旁边显示完整路径。
- 程序文件一定会删。
- 没勾"你的数据"时:删除 `app\` 里除 `data\` 以外的全部内容(包括运行时下载的 `models\` 等)。勾了:删除整个 `app\`;所选的上层目录空了也一起删除。`data\downloads\_qairt_tmp.zip` 这类下载临时文件无论如何都会删。
- 静默卸载(`/SILENT`、`/VERYSILENT`)不弹窗,什么可选内容都不删(与"全部保留"相同)。
- uv / pip 的全局缓存(`%LOCALAPPDATA%\uv\cache` 等)是机器上共享的,不删。

## 已知限制 / 未验证

- **已实测(x64 包,在 Windows-on-ARM 上模拟运行,隔离环境)**:静默安装 → `setup_env.bat` 7 步 2m38s 无错误 → `Start.bat` 9s 就绪,`/api/system/health` 200,前端 200 → 静默卸载。
- **未测**:arm64 包(原生)、有界面的向导、真正干净的机器(没有系统 Python / VC++ 运行库)、中文界面实际显示、「修复」快捷方式、代理/国内镜像页。
- **踩过的坑**:Inno 6.7 默认开 RedirectionGuard,子进程继承后 `uv python install` 建 junction 报 `os error 448`;已在 `.iss` 设 `RedirectionGuard=no`。打包白名单也曾漏掉 `tools/`(`scripts.init.install` 需要它)。
- 安装时 `setup_env.bat` 的完整输出在 `<安装目录>\data\logs\install_setup_env.log`,排错先看它。
- 安装路径含空格或非 ASCII 会给警告(部分脚本拼路径,未全面验证)。
- x64 的 `qai_appbuilder` wheel 只在 GitHub release 上,无法访问 GitHub 时会装不上(`setup_env.bat` 会给 `[ATTENTION]`)。
- `ChineseSimplified.isl` 是社区翻译(kira-96/Inno-Setup-Chinese-Simplified-Translation),Inno 官方不再自带。
