# setup_env.bat 安装内容清单(x64 / ARM64)

> 目的:改 `setup_env.bat`、`installer/`、`pyproject.toml` 时,对照本表检查有没有漏装的库或文件。
> 来源:逐行通读 `setup_env.bat`(1055 行)及其调用的脚本。**改了代码就改本文件**,
> 再运行文末的「一致性校验」,确认表里的 URL / 文件名 / 版本和代码一致。
>
> 图例:✅ 默认安装 ⚙️ 仅在对应开关下安装 ➖ 该架构不装 ⚠️ 有前提/回退
> 与 Setup.bat 的逐项对比见 **§8**;表中带 **【S】** 的行表示该处与 Setup.bat 有差异。
> **只看成品安装包(不考虑开关)与 `Setup.bat` 默认运行的差异,直接看 §9。**

## 0. 总览

| 项目 | x64 | ARM64 |
|---|---|---|
| venv | `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_x64_313` | `...\.venv_arm64_313` |
| Python | `cpython-3.13-windows-x86_64` | `cpython-3.13-windows-aarch64` |
| uv 平台标识 | `x86_64` / wheel `win_amd64` | `aarch64` / wheel `win_arm64` |
| qai_appbuilder 版本 | `2.48.40.1`(纯 x86_64 构建) | `2.48.40` |
| 架构来源优先级 | `--arch` > `data\config\host_arch` > `PROCESSOR_ARCHITECTURE` | 同左 |

安装包(`installer\`)调用方式:`setup_env.bat --arch <arch> --no-pause --with-browser --with-astgrep [--mirror ...] [--proxy ...]`,
**固定带 `--with-browser --with-astgrep`(`.iss` 的 `#define SetupComponents`,安装和"修复"快捷方式共用),不带 `--with-apps`、不带 `--builder`**。其余可选项只能手动或经 `setup_gui` 勾选。

## 1. 默认安装(不带任何开关)

按 `setup_env.bat` 的 Step 顺序。

| Step | 内容 | 用途 | x64 | ARM64 | 来源(主 → 回退) |
|---|---|---|---|---|---|
| 1 【S】 | 网络预检 `scripts\setup\net_config.ps1` | 判断 pypi / github 是否可达,选镜像、代理 | ✅ | ✅ | 本地脚本;探测 pypi.org、files.pythonhosted.org、github.com、清华镜像 等 |
| 2a | `aria2c.exe` 1.37.0 → `data\bin\aria2c\` | 多连接下载,加速后续大文件 | ✅ 非致命 | ✅ 非致命 | PATH 里的 → `github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip` |
| 2b 【S】 | `uv.exe`、`uvx.exe` → `data\bin\uv\` | 装 Python、建 venv、装依赖 | ✅ | ✅ | `data\bin\uv` → PATH → PyPI `uv` wheel(`win_amd64` / `win_arm64`)→ `github.com/astral-sh/uv/releases/latest/download/uv-<arch>-pc-windows-msvc.zip` |
| 3a 【S】 | Python 3.13 解释器 | 运行环境 | ✅ | ✅ | `uv python install`(python-build-standalone;cn 镜像 `registry.npmmirror.com/-/binary/python-build-standalone/`) |
| 3b | venv + `pip`(`--seed`)+ `pycache` 的 `.pth` | 隔离环境 | ✅ | ✅ | `uv venv`;`install_pycache_pth.py` |
| 4a 【S】 | **vendor-deps.7z**(约 201 MB,解后约 420 MB) | ARM64 专用 wheel 与数据 | ➖ | ✅ 缺 `vendor\whl\qai_appbuilder-2.48.40-...whl` 时才下;失败则**中止安装** | `https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z`;解包先 `tar.exe`,失败再 `7zr.exe`(`www.7-zip.org/a/7zr.exe`) |
| 4b 【S】 | `guard64.dll`(+ ARM64 的 `guard-injector.exe`)+ `vendor\bin\guard.source` | FileGuard 原生文件保护 | ✅ 非致命 | ✅ 非致命 | `github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/guard.zip`;`guard.source` 与该 URL 一致且 dll 在则跳过 |
| 5a | 14 个 vendored wheel(`--no-deps`)+ `_constraints.txt` | PyPI 没有 / 已撤回的 ARM64 包 | ➖ | ✅ | `vendor\whl\`(见 §3) |
| 5b 【S】 | `pyproject.toml` 核心依赖 + `--extra cc-sdk` | 服务端运行依赖 | ✅ | ✅(`--find-links vendor\whl -c _constraints.txt`) | PyPI(cn 档改用清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`) |
| 5d | `qai_appbuilder` | NPU 推理绑定 | ✅ 版本须恰为 `2.48.40.1`;失败只警告 | ✅ 版本 `2.48.40`,来自 `vendor\whl` | x64:`.../releases/download/v2.48.40/qai_appbuilder-2.48.40.1-cp313-cp313-win_amd64.whl`(**不从 PyPI**,PyPI 的 win_amd64 是 ARM64EC 构建) |
| 6 | `scripts.init.install --apply` 初始化 `data\`(DB 迁移、配置、密钥) | 首次运行数据 | ✅ | ✅ | 本地;随后 `patch_qairt_env_arch.py`(若有 `qairt_env.json`) |
| 7 | 验证 import + 写 `data\config\host_arch` | 自检 | ✅ | ✅ | 本地 |

安装后另有:`build_frontend.bat`(若 `frontend\dist` 缺失)。**安装包已带预编译前端**,所以安装包场景不需要它。

## 2. 可选项

| 开关 | 内容 | x64 | ARM64 | 来源 / 说明 |
|---|---|---|---|---|
| `--with-browser` 【S】 | `--extra search`:`playwright==1.61.0`、selectolax、lxml、cssselect、browserforge;再 `playwright install chromium`(约 150 MB,非致命) | ⚙️ | ⚙️ | PyPI;Chromium 来自 `cdn.playwright.dev` / `cdn.npmmirror.com/binaries/playwright` |
| `--with-astgrep` 【S】 | `ast-grep.exe`、`sg.exe` 0.45.1 → `vendor\bin\<arch>\`(约 50 MB × 2) | ⚙️ | ⚠️ 见 §6 | PyPI `ast-grep-cli`(`win_amd64` / `win_arm64`)→ `github.com/ast-grep/ast-grep/releases/download/0.45.1/app-<x86_64\|aarch64>-pc-windows-msvc.zip` |
| `--with-apps` 【S】 | 4 个 App Builder 包的 `requirements.txt` 聚合安装;`predeploy_tts_runtime.py`(NLTK、jieba、g2p_en 缓存) | ⚙️ | ⚙️ | PyPI(ARM64 加 `--find-links vendor\whl`);NLTK 数据来自 nltk 官方源;**聚合时跳过 `qai-appbuilder`,`openai-whisper` 用 `--no-deps`** |
| `--builder` | 模型转换环境(§4) | ⚙️ | ⚙️ | 约 2 GB QAIRT SDK + x64 Python 3.10 venv;**安装包不会触发** |
| (附加) `--extra search` + `--extra cc-sdk` | 同 `--with-browser` / 默认 | | | `--builder` 会同时启用 `search` extra |

## 3. ARM64 的 vendored 内容(`vendor-deps.7z` → `vendor\`)

`:fetch_vendor` 只拷贝:`whl`、`tiktoken`、`nltk_data`、`g2p_data`、`bin\arm64\*`(**排除** `guard64.dll`、`guard-injector.exe`)。
**不拷贝**:`openblas\`、`bin\x64\`。

### 3.1 `vendor\whl\` 中被 `:install_vendor_whls` 装入的 14 个(核心)

| wheel | 备注 |
|---|---|
| `qai_appbuilder-2.48.40-cp313-cp313-win_arm64.whl` | 不在 PyPI,与 QAIRT 版本绑定 |
| `claude_agent_sdk-0.1.72-py3-none-win_arm64.whl` | 不在 PyPI |
| `ripgrep-15.0.0-py3-none-win_arm64.whl` | |
| `kaldi_native_fbank-1.22.3-cp313-cp313-win_arm64.whl` | |
| `pyclipper-1.4.0-cp313-cp313-win_arm64.whl` | |
| `tiktoken-0.12.0-cp313-cp313-win_arm64.whl` | |
| `opencv_python_headless-4.10.0.84-cp313-cp313-win_arm64.whl` | |
| `httptools-0.7.1-cp313-cp313-win_arm64.whl` | |
| `numpy-2.3.1-cp313-cp313-win_arm64.whl` | |
| `aiohttp-3.13.5-cp313-cp313-win_arm64.whl` | **PyPI 已撤回**,这里是唯一来源 |
| `cryptography-45.0.5-cp313-abi3-win_arm64.whl` | **PyPI 已撤回** |
| `MarkupSafe-2.1.5-cp313-cp313-win_arm64.whl` | **PyPI 已撤回** |
| `soundfile-0.13.1-cp313-cp313-win_arm64.whl` | |
| `grimp-3.14-cp313-cp313-win_arm64.whl` | 仅 `dev` extra 用,但仍被装入 |

`_constraints.txt` 固定同样 14 个包的版本,供 `5b` 的 `uv pip install -r pyproject.toml` 使用。

### 3.2 `vendor\whl\` 中**存在但不被 setup_env.bat 安装**的 wheel(AIMET 相关)

`aimet_onnx-2.37.0+cpu`、`aimet_torch-2.37.0+cpu`、`osqp-1.0.1`、`scs-3.2.7`、`clarabel-0.11.1`、`cvxpy-1.6.6`,
以及 `openblas\openblas.dll`(它们的运行时依赖,**也不拷贝**)。原 `Setup.bat` Step 4h 才装;`setup_env.bat` 没有对应步骤。

### 3.3 其它被拷贝的 vendored 数据

| 目录 | 内容 | 用途 |
|---|---|---|
| `vendor\tiktoken\` | `cl100k_base`、`o200k_base` 词表(各两份文件名:URL 的 SHA1 / SHA256) | token 估算离线加载 |
| `vendor\nltk_data\` | `cmudict`、`averaged_perceptron_tagger`、`..._eng` | TTS(g2p_en) |
| `vendor\g2p_data\g2p_cache.pkl` | g2p 缓存 | TTS |
| `vendor\bin\arm64\` | `ast-grep.exe`、`sg.exe`(见 §6)、`rule\` 空目录 | |

## 4. `--builder`(模型转换环境)

| Step | 内容 | 版本 / 来源 |
|---|---|---|
| 8a | x64 Python 3.10 venv `.venv_x64_310`(`--no-project`) | `cpython-3.10-windows-x86_64` |
| 8b | QAIRT SDK → `C:\Qualcomm\AIStack\QAIRT\<ver>` | 版本取自 `scripts\qairt_release.json`(现为 `2.48.40.260702`);`vendor\qairt\v<ver>.zip` > `data\sdk\qairt\v<ver>.zip` > 下载 `https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/<ver>/v<ver>.zip`(约 2 GB);解压用 `7za.exe`(`www.7-zip.org/a/7z2301-extra.7z`),回退 `Expand-Archive` |
| 8c | 转换器依赖 → `.venv_x64_310` | `protobuf`、`numpy==1.26.4`、`ml_dtypes==0.5.1`、`onnx==1.19.1`、`onnxruntime==1.23.2`、`onnxsim==0.4.36`、`pandas`、`torch==2.11.0+cpu`、`torchvision==0.26.0+cpu`(`https://download.pytorch.org/whl/cpu`)、`onnxscript`、`tensorflow==2.21.0`、`tflite==2.18.0`、`Pillow`、`opencv-python==4.11.0.86`、`scipy`、`matplotlib`、`tqdm`、`pyyaml`、`requests` |
| 8d | 推理依赖 → 运行 venv(`--install-inference-deps --host-arch`) | 见下表 |
| 8e | `data\config\qairt_env.json` + `--verify` | 本地 |

8d 按架构不同(`setup_qairt_env.py`):

| 包 | x64 | ARM64 |
|---|---|---|
| Pillow、numpy、scipy、matplotlib、tqdm、pyyaml、requests | ✅ | ✅ |
| `opencv-python-headless` | ✅(PyPI) | ➖(用 vendored `opencv_python_headless-4.10.0.84`) |
| `MarkupSafe`、`pyclipper`、`soundfile`、`kaldi-native-fbank` | ✅(PyPI) | ➖(已由 vendored wheel 提供) |
| `onnxruntime` | ✅ | ➖ |
| `onnxruntime-qnn==1.24.4` | ➖ | ✅(PyPI) |
| `qai-appbuilder` | ⚠️ 仅当 `import qai_appbuilder` 失败时才装,装的是不带版本的 PyPI 包(见 §6) | ➖(已装) |
| `torch`、`torchvision` | ✅ PyPI | ✅ `https://download.pytorch.org/whl`(Windows 分支;只有 Linux 用 `/whl/cpu`) |

## 5. 随安装包内置的内容(`installer\build_installer.ps1`)

安装包 = 源码 + 下列文件,安装时联网只补齐其余部分。

| 内容 | x64 包 | ARM64 包 | 说明 |
|---|---|---|---|
| `src` `apps` `factory` `scripts` `interfaces` `skills` `tools`、`frontend\dist`(预编译) | ✅ | ✅ | 排除 `tests`、`node_modules`、`__pycache__`、`*.pyc`、`*.log` 等 |
| `pyproject.toml` `build_info.json` `setup_env.bat` `Start.bat` `qai.bat` `Console.bat` `Uninstall.bat` `clean_env.bat`、README / QUICK-START | ✅ | ✅ | |
| `data\bin\uv\uv.exe`、`uvx.exe` | ✅ 按架构取 | ✅ 按架构取 | 来自 PyPI wheel |
| `data\bin\aria2c\aria2c.exe` | ✅ | ✅ | x64 程序,ARM64 经模拟运行 |
| `vendor\bin\<arch>\guard64.dll`、`vendor\bin\guard.source` | ✅ | ✅ | 另 ARM64 有 `guard-injector.exe`;取自 `guard.zip` |
| `vendor\tiktoken\`(4 个文件) | ✅ | ✅ | 官方 URL 下载,校验后写入 |
| `vendor-deps.7z`、`vendor\whl` | ➖ | ➖ | **ARM64 安装时仍要联网下载 ~201 MB**(见 §6) |

## 6. 已知差异与待决事项(改动前先看)

1. **ARM64 的 ast-grep 会被默认装上**:`:fetch_vendor` 把 `bin\arm64\*`(含 `ast-grep.exe`、`sg.exe`)整体拷入 `vendor\bin\arm64`,只排除 guard 文件;而 x64 只有 `--with-astgrep` 才装。两个架构行为不一致,`--with-astgrep` 在 ARM64 上因文件已在而打印 `[SKIP]`。
2. **`--builder` 在 x64 上的 `qai-appbuilder` 回退会装 PyPI 版**:`install_inference_deps` 先用 `import` 探测,已能导入就跳过(`already installed`),所以默认流程已装好 `2.48.40.1` 时不会被覆盖。**但**若 GitHub 下载失败导致 5d 没装上,`--builder` 会改装不带版本的 PyPI 包;PyPI 的 win_amd64 是 ARM64EC 构建,在 Intel/AMD 上无法加载(见 `setup_env.bat` 顶部注释)。
3. **x64 没有 `vendor\tiktoken`**(除非用安装包):`setup_env.bat` 不预取词表,首次计数时才联网,失败回退到按长度估算。
4. **ARM64 的 `vendor-deps.7z` 与 `Setup.bat`/GUI 都从 `aidevhome.com` 下载**:单点来源,约 201 MB;这是 ARM64 安装慢/失败的主要原因。
5. **`vendor\whl` 里的 AIMET 系列与 `openblas.dll` 不被安装**(§3.2)。若以后要支持 AIMET,需补 `setup_env.bat` 步骤。
6. **7z 里的 `guard64.dll` 是旧版**(arm64 631,296 B / x64 652,288 B),必须用 GitHub `guard.zip` 的新版覆盖,所以 `:fetch_vendor` 排除它们。
7. **`ppocrv4`、`real-esrgan-x4plus`、`_shared` 没有 `requirements.txt`**,`--with-apps` 实际只聚合 `melotts-zh`、`whisper-base`、`zipformer-zh` 三个包的依赖。
8. **`melotts-zh` 的 `requirements.txt` 含 `qai_appbuilder>=2.45.0`、`whisper-base` 含 `qai-appbuilder==2.45.40`**:聚合时被 `SKIP_PKGS` 忽略,不会覆盖已装版本。

## 7. 维护清单(改动时逐条核对)

- [ ] 升级 `QAI_AB_RELEASE_TAG`:同步改 §0、§1(5d、4b)的版本与 URL;ARM64 还要换 `vendor-deps.7z` 里的 wheel 和 `:install_vendor_whls`、`_constraints.txt` 的版本;**重新打安装包**(`guard.source` 里的 URL 含版本)。
- [ ] 新增/升级 vendored wheel:改 `:install_vendor_whls` 的包列表**和** `_constraints.txt` 的 `echo` 列表(两处各一份),并更新 §3.1。
- [ ] 新增 pyproject 依赖:PyPI 上若缺 `win_arm64` wheel,要补进 vendored 列表,否则 ARM64 安装会失败。
- [ ] 改 `--with-*` 或 `--builder` 的包:同步 §2、§4。
- [ ] 改安装包内容:同步 `build_installer.ps1` 的 `$Dirs` / `$Files` 白名单和 §5、`installer\README.md`。
- [ ] 改 `Setup.bat`:它和 `setup_env.bat` 共用 `vendor\bin\guard.source` 标记,别让两者互相重复下载。
- [ ] 新增外部下载:补 §1/§2 的「来源」,并确认 `net_config.ps1` 的预检/镜像是否需要覆盖该域名。

## 8. 与 `Setup.bat` 的对比

`Setup.bat`(3251 行)是原有的完整安装入口;`setup_env.bat`(1055 行)是精简、容忍网络问题的版本。
二者**写入同一布局**(venv、`data\`、`host_arch`),所以 `Start.bat` / `qai.bat` / `Console.bat` 对两者的结果都能用。
已核对:**14 个 vendored wheel 的清单和 `_constraints` 的 14 条版本完全一致**;`aria2c` 1.37.0、`qai_appbuilder` 2.48.40 也一致。

### 8.1 默认行为的差异(不带任何开关)

| 项目 | `Setup.bat` | `setup_env.bat` | 影响 |
|---|---|---|---|
| **x64 下载 `vendor-deps.7z`(约 201 MB)** | ✅ **会下**(Step 0b 没有架构判断,x64 也下) | ➖ 不下 | x64 上 `Setup.bat` 白白多下约 200 MB,是 x64 安装慢的主要原因之一 |
| ARM64 下载 `vendor-deps.7z` | ✅,**失败只警告**,继续 | ✅,**失败则中止安装** | `setup_env.bat` 更严格;ARM64 没有它就装不了 |
| Playwright 浏览器 | ✅ **默认装**:`--extra search` + `playwright install chromium`(约 150 MB) | ⚙️ 仅 `--with-browser` | `Setup.bat` 默认多装约 150 MB |
| ast-grep | ✅ **默认装**(Step 0d,两架构各自的 HOST_ARCH 版本) | ⚙️ 仅 `--with-astgrep`(但 ARM64 因 7z 内含而默认有,见 §6) | `Setup.bat` 的 x64 默认多装约 50 MB |
| App Builder 包依赖(whisper/melotts/zipformer) | ✅ **默认装**(Step 4e) | ⚙️ 仅 `--with-apps` | |
| TTS 运行数据(NLTK / jieba / g2p_en) | ✅ **默认跑**(Step 6) | ⚙️ 仅 `--with-apps` | |
| WebView2 Fixed Version(约 811 MB 复制) | ✅ **默认**(Step 10),仅当系统 Evergreen 注册表缺失才复制 | ➖ 没有 | 桌面壳(Tauri)专用;浏览器版不需要 |
| VC++ 运行库 | ✅ 探测失败才下载 `https://aka.ms/vs/17/release/vc_redist.<arch>.exe` 并静默安装 | ➖ 没有 | 干净的 x64 机器上,`setup_env.bat` 可能在建 venv 时失败(`0xc0e90002`) |
| WDAC / 代码完整性策略下的签名 Python | ✅ `ensure_signed_python.ps1`:受限机器上改装 python.org 签名版(3.13.14;`--builder` 的 3.10.11) | ➖ 没有 | 企业受限机器上 `setup_env.bat` 可能直接失败(os error 448) |
| git pre-commit 钩子 | ✅ 在 git 工作区里设置 `core.hooksPath=.githooks` | ➖ 没有 | 仅开发者相关 |
| `pip` 升级 | ✅ `ensurepip --upgrade` + `pip install --upgrade pip` | ➖ 不做(只用 `--seed`) | |
| 额外探测的 import(自愈) | ✅ 探测 `playwright, selectolax, lxml, browserforge, croniter` 等 | 只探测 `fastapi, uvicorn, structlog, paramiko, claude_agent_sdk` | 见 §8.4 第 3 条 |
| 重新安装 `qai_appbuilder` | ✅ **每次都强制重装**(ARM64);x64 版本不符才装 | 版本恰好相符就跳过(两架构) | `setup_env.bat` 更快 |

### 8.2 开关对照

| 作用 | `Setup.bat` | `setup_env.bat` |
|---|---|---|
| 不暂停 | `--no-pause` | `--no-pause` |
| 指定架构 | `--arch arm64\|x64` | `--arch arm64\|x64` |
| 模型转换环境 | `--builder`(默认关);`--no-builder` | `--builder`(默认关) |
| 开发/测试依赖 | `--dev`(`dev` + `e2e` extras) | **没有** |
| 预下载语音模型权重 | `--voice-models`(whisper_medium / zipformer / melotts_zh,v0.55.0,来自 `qaihub-public-assets.s3.us-west-2.amazonaws.com`,按 SoC 选 X Elite / X2 Elite) | **没有**(运行时首次使用才下) |
| PortableGit 2.54.0 | `--git`(`github.com/git-for-windows/...`;只给 exec 的 `shell='sh'` 用) | **没有** |
| Node.js 22.20.0 + pnpm 11.9.0 | `--frontend`(`nodejs.org`;给 `Build.bat` 用) | **没有**(前端由单独的 `build_frontend.bat` 装) |
| 桌面构建工具链(Rust + tauri-cli) | `--desktop`(隐含 `--builder`;`win.rustup.rs`) | **没有** |
| 以上全部 | `--full` | **没有** |
| 浏览器工具 | (默认装) | `--with-browser` |
| ast-grep | (默认装) | `--with-astgrep` |
| App Builder 依赖 | (默认装) | `--with-apps` |
| 网络配置 | 只读环境变量 `HTTPS_PROXY` / `ALL_PROXY` / `HTTP_PROXY` | `--proxy URL`、`--mirror auto\|official\|cn`、`--no-probe` + 预检 |

### 8.3 下载来源的差异

| 内容 | `Setup.bat` | `setup_env.bat` |
|---|---|---|
| `uv` | **只从 GitHub**:`github.com/astral-sh/uv/releases/latest/download/uv-<arch>-pc-windows-msvc.zip` | PyPI wheel(`pypi.org` / 清华镜像)→ 回退 GitHub |
| `ast-grep` | 只从 GitHub release 0.45.1 | PyPI `ast-grep-cli` 0.45.1 → 回退 GitHub |
| PyPI 依赖 | 默认源;**无重试、无镜像切换** | cn 档用清华镜像;网络错误会重试并**在 cn/official 之间切换**(`:uv_retry`) |
| Python 解释器 | 默认源 | cn 档用 `registry.npmmirror.com` 的 python-build-standalone 镜像 |
| GitHub 大文件 | 直连 | 直连失败后自动选一个 GitHub 代理(`ghfast.top` 等)重试 |
| `guard.zip` | **每次都要下**:两个 DLL + ARM64 注入器**三个都在**才跳过 | 只要本架构的 DLL 在且 `guard.source` 标记一致就跳过(安装包已内置,**安装时不下载**) |
| `vendor-deps.7z` 解包 | 只用 `7zr.exe`(需先下载) | 先用系统自带 `tar.exe`,失败才用 `7zr.exe` |
| 7z 里 `guard64.dll` | 解包后整体合并进 `vendor\`,随后被 `guard.zip` 的新版覆盖 | **合并时排除** guard 文件 |
| 7z 里其余内容 | **整体合并**(含 `openblas`、`bin\x64`、`bin\arm64`) | 只拷 `whl`、`tiktoken`、`nltk_data`、`g2p_data`、`bin\arm64`(**不拷** `openblas`、`bin\x64`) |

### 8.4 值得注意的差异(改动前先看)

1. **x64 + `Setup.bat` 会下载 201 MB 的 ARM64 依赖包**。这包里 x64 用得上的只有 `tiktoken` 词表和 NLTK/g2p 数据(约 40 MB)。若以后要精简 `Setup.bat`,可给 `:fetch_vendor_deps` 加架构判断。
2. **默认装的东西不同**(Setup.bat 的 Chromium 与 Pack 依赖在「依赖已完整」时会被整体跳过,所以重复运行不会重装;首次安装才会装):`Setup.bat` 默认装 Playwright + Chromium、ast-grep、App Builder 依赖、TTS 数据;`setup_env.bat` 全部改为可选。二者产出的环境**功能并不等价**——用 `setup_env.bat` 装完,浏览器搜索、`ast_grep` 工具(x64)、语音/OCR 模型可能不可用,需要补开关。
3. **自愈探测不同**:`Setup.bat` 看到 `playwright` / `selectolax` / `lxml` / `browserforge` / `croniter` 缺失时会重装;`setup_env.bat` 的探测更少。若升级 `pyproject.toml` 新增了运行时依赖,`setup_env.bat` 的 `[SKIP] dependency closure already importable` 可能**漏掉**它——新增依赖时要同步改这条探测。
4. **`setup_env.bat` 没有的保护**:VC++ 运行库、WDAC 签名 Python、`pip` 升级。受限或全新的 x64 机器上,`setup_env.bat` 失败的概率更高,而 `Setup.bat` 会自己修复。
5. **ARM64 的 `vendor-deps.7z` 失败的处理相反**:`Setup.bat` 只警告然后让 `uv` 回退 PyPI(ARM64 上多半装不上被撤回的 wheel);`setup_env.bat` 直接中止并提示手动下载。
6. **`--builder` 的 VS 工具**:`Setup.bat` 的 Step 8.3 会静默安装 Visual Studio 2022 Community(`aka.ms/vs/17/release/vs_community.exe`,含 MSVC x64/ARM64、CMake、clang-cl、Windows 11 SDK 22621),体积很大;`setup_env.bat --builder` 明确**不装**(只支持 DLC 流程,不需要编译器)。

### 8.5 `Setup.bat` 独有的下载项(`setup_env.bat` 不会下)

| 内容 | 触发 | 来源 | 大小 |
|---|---|---|---|
| VC++ Redistributable | 建 venv 探测失败时 | `https://aka.ms/vs/17/release/vc_redist.<arch>.exe` | 约 5 MB+ |
| python.org 签名版 Python | 检测到 WDAC 强制策略时 | `https://www.python.org/ftp/python/<ver>/python-<ver>-<arch>.exe` | |
| Playwright Chromium | 默认 | Playwright CDN | 约 150 MB |
| ast-grep | 默认 | GitHub release 0.45.1 | 约 50 MB |
| PortableGit 2.54.0 | `--git` / `--full` | `github.com/git-for-windows/git/releases/download/v2.54.0.windows.1/PortableGit-2.54.0-<64-bit\|arm64>.7z.exe`;7zr 解包 | 约 57 MB |
| Node.js 22.20.0 | `--frontend` / `--full` | `https://nodejs.org/dist/v22.20.0/node-v22.20.0-win-<x64\|arm64>.zip` + corepack 取 pnpm 11.9.0 | |
| 语音模型(3 个 zip) | `--voice-models` / `--full` | `qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/<name>/releases/v0.55.0/...` | 较大 |
| Rust + tauri-cli | `--desktop` | `https://win.rustup.rs/<x86_64\|aarch64>`;`cargo install tauri-cli` | 约 600 MB + 300 MB |
| Visual Studio 2022 Community | `--builder` | `https://aka.ms/vs/17/release/vs_community.exe` | 很大 |
| WebView2 Fixed Version | 默认(仅系统注册表缺失时) | 从系统 `C:\Program Files (x86)\Microsoft\EdgeWebView\Application\<ver>` **复制**,不下载 | 约 811 MB |
## 9. 成品安装包 vs `Setup.bat`(默认运行,不考虑开关)

口径:安装包 = Inno 向导把文件放到 `{app}\app`,然后固定执行 `setup_env.bat --arch <arch> --no-pause --with-browser --with-astgrep`(向导另外只会加 `--mirror` / `--proxy`,**不带 `--with-apps` / `--builder`**)。
对照对象 = 在源码目录里直接运行 `Setup.bat`(不加任何参数)。两边都以同一架构比较。

### 9.1 两边一致

venv(`.venv_<arch>_313`)、Python 3.13、`uv`、`aria2c`、`pyproject.toml` 核心依赖 + `cc-sdk`、`qai_appbuilder`(x64 `2.48.40.1` 来自 GitHub release;ARM64 `2.48.40` 来自 vendored wheel)、
ARM64 的 14 个 vendored wheel、`data\` 初始化、`host_arch`、`guard64.dll`、`tiktoken` 词表。
**`search` extra + Chromium**(安装包经 `--with-browser`)和 **ast-grep**(安装包经 `--with-astgrep`,来自 PyPI `ast-grep-cli`;`Setup.bat` 来自 GitHub release)。

### 9.2 安装包**多出来**或更好的地方

| 项目 | 安装包 | `Setup.bat` 默认 |
|---|---|---|
| 前端 `frontend\dist` | ✅ 打包时已编译好 | ➖ **不构建**,要另跑 `Build.bat`(`Setup.bat` 本身不装 Node) |
| `guard64.dll` | ✅ 内置本架构的(安装时不下载) | 每次都下载 `guard.zip`(约 30 MB) |
| `tiktoken` 词表 | ✅ 内置(x64 也有) | x64 靠下载 201 MB 的 `vendor-deps.7z` 才有 |
| x64 的 `vendor-deps.7z`(201 MB) | ➖ 不下 | ✅ 会下 |
| 网络容错 | ✅ 向导选源/代理;PyPI wheel 取 uv;cn/official 自动切换;GitHub 代理回退 | ➖ 无 |
| 安装体验 | ✅ 向导、进度条、开始菜单/桌面快捷方式、修复入口、卸载 | ➖ 命令行 |
| 卸载时可选删除 Chromium(`%LOCALAPPDATA%\ms-playwright`,实测 688 MB) | ✅ 卸载窗口里单独一项 | `Uninstall.bat` 直接删 |

### 9.3 安装包**缺少**(`Setup.bat` 默认会装)—— 功能差异

| 项目 | `Setup.bat` 默认 | 安装包 | 缺少后的表现 |
|---|---|---|---|
| **App Builder 包依赖**(`melotts-zh`、`whisper-base`、`zipformer-zh` 的 `requirements.txt`:`jieba`、`pypinyin`、`cn2an`、`g2p_en`、`openai-whisper`、`scipy` 等) | ✅ | ➖ | `melotts-zh` 的 `runner.py` / `predeploy.py` 直接 `import nltk / jieba / g2p_en`,没装会 ImportError,语音/TTS 模型跑不起来 |
| **TTS 运行数据**(NLTK `cmudict`、`averaged_perceptron_tagger*`、`g2p_cache.pkl`) | ✅(`predeploy_tts_runtime.py`) | x64 ➖;ARM64 ✅(7z 内带) | x64 首次 TTS 推理需联网下载,离线则失败 |
| **VC++ 运行库** 探测与安装 | ✅ | ➖ | 全新的 x64 机器上建 venv 可能报 `0xc0e90002` |
| **WDAC 受限机器**的签名 Python | ✅ | ➖ | 企业受限机器可能报 os error 448,安装失败 |
| `pip` 升级 | ✅ | ➖ | 无功能影响 |
| 自愈探测 `playwright / selectolax / lxml / browserforge / croniter` | ✅ | 安装包带 `--with-browser`,所以每次都会跑 `uv pip install`(不走 `[SKIP] dependency closure`),不会漏装 | 无 |
| `qai_appbuilder` 每次强制重装 | ✅(ARM64) | 版本相符就跳过 | 安装包更快,无功能影响 |

### 9.4 两边都不装(都需要手动加开关/另行安装)

模型转换环境(QAIRT SDK、x64 Python 3.10、VS 2022)、PortableGit、Node.js + pnpm、Rust + tauri-cli、语音模型权重(运行时首次使用才下)、`--dev` 测试依赖。

其中 **PortableGit**:`exec` 工具的 `shell='sh'` 需要它;没有时代码返回明确的"未找到 PortableGit,请运行 `Setup.bat --git`"提示,`cmd` / `powershell` 不受影响。

### 9.5 与桌面壳相关、安装包不需要的项

`Setup.bat` 的 **WebView2 Fixed Version**(最多复制约 811 MB)和 git pre-commit 钩子(`.githooks` 未随包发布)只服务桌面壳/开发者。安装包的快捷方式指向 `Start.bat`(浏览器版),不使用 WebView2。

### 9.6 安装包未随包提供的脚本

`Setup.bat`、`Build.bat`、`build_frontend.bat`、`setup_gui.bat` 不在白名单里。装完后无法在安装目录里重新构建前端,也没有 `Setup.bat` 的 `--git` / `--voice-models` 等入口;"修复"快捷方式只会重跑 `setup_env.bat`。

### 9.7 结论:要让安装包追平 `Setup.bat` 默认体验,还缺的是

1. App Builder 包依赖 + TTS 数据(语音、TTS 模型);对应 `setup_env.bat --with-apps`,安装包目前没带;
2. VC++ 运行库与 WDAC 回退(受限/全新机器的稳健性);`setup_env.bat` 没有实现。

浏览器(`search` extra + Chromium)和 ast-grep 已由安装包固定追加 `--with-browser --with-astgrep` 补齐。

## 10. 一致性校验

在 `tools\qaiappbuilder` 下运行。每项都应输出 `OK`;出现 `MISSING` 说明文档和代码已脱节。

```powershell
$bat = Get-Content setup_env.bat -Raw
$doc = Get-Content docs\setup-install-inventory.md -Raw
$ok = $true
function Check($name, $cond) { if ($cond) { "OK       $name" } else { "MISSING  $name"; $script:ok = $false } }

# 1) 代码里的 URL 都出现在文档里
[regex]::Matches($bat, 'https://[^\s"''^)%]+') | ForEach-Object { $_.Value.TrimEnd('.',',') } | Sort-Object -Unique |
  Where-Object { $_ -notmatch '%|\$' } | ForEach-Object {
    $host_path = ($_ -replace '^https://', '') -replace '/[^/]*$', ''
    Check "URL documented: $_" ($doc.Contains($host_path))
  }

# 2) 14 个 vendored wheel 都在文档里
[regex]::Matches($bat, '%W%\\([^"]+\.whl)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique |
  ForEach-Object { Check "vendored wheel documented: $_" ($doc.Contains($_)) }

# 3) 版本号
foreach ($v in 'QAI_AB_RELEASE_TAG=([0-9.]+)', 'release-([0-9.]+)/aria2', 'ast-grep/releases/download/([0-9.]+)/') {
  $m = [regex]::Match($bat, $v); if ($m.Success) { Check "version $($m.Groups[1].Value)" ($doc.Contains($m.Groups[1].Value)) }
}
Check "QAIRT version" ($doc.Contains((Get-Content scripts\qairt_release.json -Raw | ConvertFrom-Json).qairt_version))

# 4) 开关
foreach ($s in '--with-browser','--with-astgrep','--with-apps','--builder') { Check "switch documented: $s" ($doc.Contains($s)) }

# 5) _constraints.txt 与 wheel 列表条数一致
$cons = ([regex]::Matches($bat, 'echo ([A-Za-z_\-]+)==') | Measure-Object).Count
$whls = ([regex]::Matches($bat, '%W%\\[^"]+\.whl') | Select-Object -Unique | Measure-Object).Count
Check "constraints ($cons) == vendored wheels ($whls)" ($cons -eq $whls)

# 6) 与 Setup.bat 的对照(§8):14 个 wheel 清单与约束必须一致
if (Test-Path Setup.bat) {
  $sb = Get-Content Setup.bat -Raw
  $wa = [regex]::Matches($sb, 'vendor\\whl\\([^"]+\.whl)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
  $wb = [regex]::Matches($bat, '%W%\\([^"]+\.whl)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
  Check "Setup.bat and setup_env.bat vendor the same wheels" (-not (Compare-Object $wa $wb))
  foreach ($k in 'GIT_VERSION', 'NODE_VERSION', 'PNPM_VERSION', 'ASTGREP_VERSION', 'SIGNED_PY_VERSION', 'SIGNED_PY310_VERSION') {
    $v = [regex]::Match($sb, "set `"$k=([^`"]+)`"").Groups[1].Value
    Check "Setup.bat $k=$v documented" ($v -and $doc.Contains($v))
  }
  Check "Setup.bat Step 0b has no arch gate (x64 downloads vendor-deps.7z)" `
    (-not ((Get-Content Setup.bat)[2416..2535] | Select-String -Pattern 'HOST_ARCH' -Quiet))
}
# 7) 安装包口径(§9):固定追加 --with-browser --with-astgrep,不带 --with-apps/--builder;修复快捷方式用同一份;内置 guard 与 tiktoken
if (Test-Path installer\qaimodelbuilder.iss) {
  $iss = Get-Content installer\qaimodelbuilder.iss -Raw
  $comp = [regex]::Match($iss, '#define SetupComponents "([^"]*)"').Groups[1].Value
  Check "installer components = --with-browser --with-astgrep ($comp)" ($comp -eq '--with-browser --with-astgrep')
  $sa = [regex]::Match($iss, '(?s)function SetupArgs.*?\bend;').Value
  Check "installer SetupArgs uses SetupComponents, no --with-apps/--builder" ($sa -match '\{#SetupComponents\}' -and ($sa -notmatch '--with-apps|--builder'))
  Check "repair shortcut uses SetupComponents" ($iss -match 'RepairName\}.*\{#SetupComponents\}')
  $bi = Get-Content installer\build_installer.ps1 -Raw
  Check "installer bundles guard + tiktoken" (($bi -match 'guard\.source') -and ($bi -match 'vendor\\tiktoken'))
  Check "installer does not ship Setup.bat" ($bi -notmatch "'Setup\.bat'")
}

if (-not $ok) { exit 1 }
```
