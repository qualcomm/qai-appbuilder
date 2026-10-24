# setup_env.bat 安装内容清单(x64 / ARM64)

> 目的:改 `setup_env.bat`、`installer/`、`pyproject.toml` 时,对照本表检查有没有漏装的库或文件。
> 来源:逐行通读 `setup_env.bat`(1055 行)及其调用的脚本。**改了代码就改本文件**,
> 再运行文末的「一致性校验」,确认表里的 URL / 文件名 / 版本和代码一致。
>
> 图例:✅ 默认安装 ⚙️ 仅在对应开关下安装 ➖ 该架构不装 ⚠️ 有前提/回退

## 0. 总览

| 项目 | x64 | ARM64 |
|---|---|---|
| venv | `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_x64_313` | `...\.venv_arm64_313` |
| Python | `cpython-3.13-windows-x86_64` | `cpython-3.13-windows-aarch64` |
| uv 平台标识 | `x86_64` / wheel `win_amd64` | `aarch64` / wheel `win_arm64` |
| qai_appbuilder 版本 | `2.48.40.1`(纯 x86_64 构建) | `2.48.40` |
| 架构来源优先级 | `--arch` > `data\config\host_arch` > `PROCESSOR_ARCHITECTURE` | 同左 |

安装包(`installer\`)调用方式:`setup_env.bat --arch <arch> --no-pause [--mirror ...] [--proxy ...]`,
**不带任何可选开关、不带 `--builder`**。可选项只能手动或经 `setup_gui` 勾选。

## 1. 默认安装(不带任何开关)

按 `setup_env.bat` 的 Step 顺序。

| Step | 内容 | 用途 | x64 | ARM64 | 来源(主 → 回退) |
|---|---|---|---|---|---|
| 1 | 网络预检 `scripts\setup\net_config.ps1` | 判断 pypi / github 是否可达,选镜像、代理 | ✅ | ✅ | 本地脚本;探测 pypi.org、files.pythonhosted.org、github.com、清华镜像 等 |
| 2a | `aria2c.exe` 1.37.0 → `data\bin\aria2c\` | 多连接下载,加速后续大文件 | ✅ 非致命 | ✅ 非致命 | PATH 里的 → `github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip` |
| 2b | `uv.exe`、`uvx.exe` → `data\bin\uv\` | 装 Python、建 venv、装依赖 | ✅ | ✅ | `data\bin\uv` → PATH → PyPI `uv` wheel(`win_amd64` / `win_arm64`)→ `github.com/astral-sh/uv/releases/latest/download/uv-<arch>-pc-windows-msvc.zip` |
| 3a | Python 3.13 解释器 | 运行环境 | ✅ | ✅ | `uv python install`(python-build-standalone;cn 镜像 `registry.npmmirror.com/-/binary/python-build-standalone/`) |
| 3b | venv + `pip`(`--seed`)+ `pycache` 的 `.pth` | 隔离环境 | ✅ | ✅ | `uv venv`;`install_pycache_pth.py` |
| 4a | **vendor-deps.7z**(约 201 MB,解后约 420 MB) | ARM64 专用 wheel 与数据 | ➖ | ✅ 缺 `vendor\whl\qai_appbuilder-2.48.40-...whl` 时才下;失败则**中止安装** | `https://www.aidevhome.com/data/adh2/attr/vendor-deps.7z`;解包先 `tar.exe`,失败再 `7zr.exe`(`www.7-zip.org/a/7zr.exe`) |
| 4b | `guard64.dll`(+ ARM64 的 `guard-injector.exe`)+ `vendor\bin\guard.source` | FileGuard 原生文件保护 | ✅ 非致命 | ✅ 非致命 | `github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/guard.zip`;`guard.source` 与该 URL 一致且 dll 在则跳过 |
| 5a | 14 个 vendored wheel(`--no-deps`)+ `_constraints.txt` | PyPI 没有 / 已撤回的 ARM64 包 | ➖ | ✅ | `vendor\whl\`(见 §3) |
| 5b | `pyproject.toml` 核心依赖 + `--extra cc-sdk` | 服务端运行依赖 | ✅ | ✅(`--find-links vendor\whl -c _constraints.txt`) | PyPI(cn 档改用清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`) |
| 5d | `qai_appbuilder` | NPU 推理绑定 | ✅ 版本须恰为 `2.48.40.1`;失败只警告 | ✅ 版本 `2.48.40`,来自 `vendor\whl` | x64:`.../releases/download/v2.48.40/qai_appbuilder-2.48.40.1-cp313-cp313-win_amd64.whl`(**不从 PyPI**,PyPI 的 win_amd64 是 ARM64EC 构建) |
| 6 | `scripts.init.install --apply` 初始化 `data\`(DB 迁移、配置、密钥) | 首次运行数据 | ✅ | ✅ | 本地;随后 `patch_qairt_env_arch.py`(若有 `qairt_env.json`) |
| 7 | 验证 import + 写 `data\config\host_arch` | 自检 | ✅ | ✅ | 本地 |

安装后另有:`build_frontend.bat`(若 `frontend\dist` 缺失)。**安装包已带预编译前端**,所以安装包场景不需要它。

## 2. 可选项

| 开关 | 内容 | x64 | ARM64 | 来源 / 说明 |
|---|---|---|---|---|
| `--with-browser` | `--extra search`:`playwright==1.61.0`、selectolax、lxml、cssselect、browserforge;再 `playwright install chromium`(约 150 MB,非致命) | ⚙️ | ⚙️ | PyPI;Chromium 来自 `cdn.playwright.dev` / `cdn.npmmirror.com/binaries/playwright` |
| `--with-astgrep` | `ast-grep.exe`、`sg.exe` 0.45.1 → `vendor\bin\<arch>\`(约 50 MB × 2) | ⚙️ | ⚠️ 见 §6 | PyPI `ast-grep-cli`(`win_amd64` / `win_arm64`)→ `github.com/ast-grep/ast-grep/releases/download/0.45.1/app-<x86_64\|aarch64>-pc-windows-msvc.zip` |
| `--with-apps` | 4 个 App Builder 包的 `requirements.txt` 聚合安装;`predeploy_tts_runtime.py`(NLTK、jieba、g2p_en 缓存) | ⚙️ | ⚙️ | PyPI(ARM64 加 `--find-links vendor\whl`);NLTK 数据来自 nltk 官方源;**聚合时跳过 `qai-appbuilder`,`openai-whisper` 用 `--no-deps`** |
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

## 8. 一致性校验

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

if (-not $ok) { exit 1 }
```
