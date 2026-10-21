# QAI AppBuilder 安装器与安装内容(草案)

> 状态:**草案,供讨论**。内容基于对 `Setup.bat`、`setup.sh`、`scripts/setup/*`、`pyproject.toml`、`vendor/whl/README.md` 的阅读整理,未实际运行安装流程。
> 标注 **【待确认】** 的条目是没有核实过的推断或未读到的部分。

---

## 1. 背景与目标

### 1.1 现状问题
- Windows 上执行 `Setup.bat` 经常因下载依赖失败,或耗时过长。
- 下载源基本都在境外:GitHub、PyPI、python-build-standalone、Playwright CDN、raw.githubusercontent.com、softwarecenter.qualcomm.com。另有一个个人站点 `www.aidevhome.com`(托管 `vendor-deps.7z`)。
- 镜像/代理只有零散支持:
  - `download_with_aria2c.ps1` 有 `-ProxyUrl` 参数,但 `Setup.bat` 只有语音模型一步会传入。
  - uv、Playwright 的镜像变量未设置。
- Linux 的 `setup.sh` 与 Windows 的 `Setup.bat` 行为差异大(见第 5 节)。

### 1.2 目标
1. 提供图形化安装包(Windows 优先,Linux 次之)。
2. 安装过程中可配置**代理**和**镜像**。
3. 支持**在线小包**和**完整离线包**两种发布形态。
4. 默认只装运行预构建模型所需的最小集合,重型组件可选。
5. Windows / Linux 共用同一份网络配置。

### 1.3 非目标(本期)
- 不改变应用本身的运行时行为。
- 不把 QAIRT SDK(约 2GB)和 VS Build Tools 放进默认安装包。

---

## 2. 现有安装内容清单

### 2.1 Windows(`Setup.bat`)

默认安装:

| 步骤 | 内容 | 来源 | 大小 | 用途 | 失败后果 |
|---|---|---|---|---|---|
| 0 | aria2c 1.37 | GitHub | 约 5MB | 多线程下载器,后续下载都经它 | 非致命,退回 PowerShell 单线程 |
| 0b | 7zr.exe | 7-zip.org | 约 600KB | 解压 7z | 非致命 |
| 0b | vendor-deps.7z | www.aidevhome.com | 约 420MB(解压后:`whl` 170 + `bin` 199 + `openblas` 14 + `nltk_data` 20 + `tiktoken` 10 + `g2p_data` 8) | ARM64 专用 wheel、guard/ast-grep、g2p/nltk/tiktoken 离线数据 | 脚本标为非致命;ARM64 上后续大概率失败(见 3.1) |
| 0c | guard.zip(`guard64.dll`、`guard-injector.exe`) | GitHub release | 小 | 原生 FileGuard 文件保护 | 非致命,功能失效 |
| 0d | ast-grep 0.45.1 | GitHub release | 约 51MB | `ast_grep` / `ast_edit` 工具 | 非致命,工具不可用 |
| 1 | uv(latest) | GitHub release | 约 20MB | Python 环境管理 | 致命 |
| 2 | Python 3.13 | uv 下载 python-build-standalone | 约 30MB | 运行时解释器 | 致命 |
| 2b | VC++ Redist | aka.ms | 约 15MB | 仅探测失败时下载 | 仅警告 |
| 2b | 签名版 Python | python.org | 约 25MB | 仅 WDAC 策略拦截时 | 致命 |
| 4b | pyproject 依赖(含 `cc-sdk`、`search` extra) | PyPI | 数百 MB | fastapi、sqlalchemy、paramiko、lark-oapi、wechatbot-sdk、playwright、claude-agent-sdk 等 | 致命 |
| 4d | Playwright Chromium | Playwright CDN | 约 150MB | 浏览器搜索引擎、`browser` 工具 | 非致命 |
| 4e | App Builder Pack 依赖 | PyPI | 中等 | whisper / melotts / zipformer 所需:scipy、jieba、pypinyin、cn2an、g2p_en、openai-whisper 等 | 非致命 |
| 4e 后 | qai_appbuilder wheel | ARM64:vendor;x64:GitHub release | 中等 | QNN 推理绑定 | x64 下失败则推理不可用 |
| 6 | NLTK 数据 + g2p_en 预热 | NLTK 数据站 | 几十 MB | melotts 英文 G2P | 非致命 |
| 10 | WebView2 | 本机复制 | 约 811MB | 桌面版渲染 | 仅系统 Evergreen 注册异常时复制 |

可选组件(需参数):

| 参数 | 内容 | 体量 |
|---|---|---|
| `--builder` | QAIRT SDK、x64 Python 3.10 转换 venv、VS 2022 Build Tools、converter 依赖 | 约 2GB 以上 |
| `--voice-models` | Whisper / Zipformer / MeloTTS 权重 | 大(qaihub S3) |
| `--git` | PortableGit | 约 57MB |
| `--frontend` | Node.js 22 + pnpm | 中等 |
| `--desktop` | Rust + tauri-cli(隐含 `--builder`) | 约 600MB + 编译 5–10 分钟 |
| `--dev` | pytest / mypy / ruff 等 | 中等 |
| `--full` | builder + voice + git + frontend | — |

### 2.2 Linux(`setup.sh`)

| 步骤 | 内容 | 来源 |
|---|---|---|
| 前置 | 系统 Python 3.12(不自动安装) | apt |
| 前置(aarch64) | cmake、build-essential、python3.12-dev、FastRPC 包 | apt |
| 5 | `envs/venv` | 本地 |
| 6 | `pip install -e ".[dev,e2e]"` | PyPI |
| 8–9 | Node ≥ 22 + pnpm,仅 `--frontend` | nodejs.org、npm |
| 10 | 初始化 `data/`(Fernet 文件密钥后端) | 本地 |
| 10b | QAIRT SDK(约 2GB,macOS 跳过),可用 `vendor/qairt/v<version>.zip` 预置 | Qualcomm 软件中心 |
| 11b | x86_64:Python 3.10 转换器 venv | PyPI |
| 11c | aarch64:`venv_aarch64_312` 推理 venv | PyPI |
| 11d/11e | QAIRT Python 依赖校验;onnx、onnxruntime、onnxsim | PyPI |

【待确认】`scripts/init/install_qairt.py` 的实际下载地址与校验方式、`node_bootstrap.sh` 后半段的 Node 下载源,均未读。

---

## 3. 各组件是否必需

### 3.1 vendor-deps.7z

#### 3.1.1 压缩包实际内容
已下载到 `C:\work\robot\vendor_deps` 核对(共 6 个目录):

| 目录 | 大小 | 内容 | 用途 |
|---|---|---|---|
| `whl` | 170MB,21 个文件 | 14 个核心 ARM64 wheel + 6 个 AIMET 相关 wheel + README | 见 3.1.2 |
| `bin` | 199MB | `arm64\`、`x64\` 各一套:`ast-grep.exe`、`sg.exe`、`guard64.dll`、`rule`(空文件);另有 `arm64\guard-injector.exe` | `ast_grep`/`ast_edit` 工具;FileGuard 原生保护 |
| `nltk_data` | 20MB | `cmudict`、`averaged_perceptron_tagger`、`averaged_perceptron_tagger_eng`,另有 `.predeploy_ok` | melotts 英文 G2P |
| `g2p_data` | 8MB | `g2p_cache.pkl` | G2P 预生成缓存,加速加载 |
| `tiktoken` | 10MB | `cl100k_base`、`o200k_base` 词表 + `download_bpe_files.py` | 对话压缩触发时的精确 token 计数,离线加载 |
| `openblas` | 14MB | `openblas.dll`(手工编译,启用 LAPACK,win_arm64)+ README | `scs`/`osqp`/`cvxpy` 运行时加载,这些库只给 AIMET 用 |

发现的问题:
- `bin` 里已含 `ast-grep` 和 `guard64.dll`,但 `Setup.bat` 的 Step 0c、0d 又单独下载同样的东西,**重复**。
- 包里带了 `.predeploy_ok` 哨兵,`Setup.bat` 合并时会删掉它,导致 Step 6 必定重新联网下载 NLTK,设计上自相矛盾。
- **AIMET 的 6 个 wheel 和 `openblas.dll` 目前没有安装步骤用到它们**:`Setup.bat` 中搜不到 `openblas`/`aimet`/`Step 4h`(`vendor/whl/README.md` 提到的 Step 4h 看起来已被删除)。只查了 `.bat` 和 `scripts/**/*.py`,未查 `src/`,是否可移除 **【待确认】**。

#### 3.1.2 各 wheel 是否真的需要(ARM64 实测)

> 此前文档和 `vendor/whl/README.md` 都写"PyPI 没有"。**已逐个核实,其中一部分是过时的。**
> 测试环境:ARM64 Windows、uv 0.13.1、Python 3.13.15 aarch64,装机有 VS 2022(无 Rust、cmake、ninja)。设置了 `UV_SYSTEM_CERTS=1`(不设则公司代理证书导致 PyPI 全部连接失败)。
> 每个包测 4 种方式(均 `--no-deps`):**L** 本地 wheel;**P1** PyPI 钉死版本、仅二进制;**P2** PyPI 最新版、仅二进制;**P3** PyPI 钉死版本、允许 sdist 构建。传递依赖单独补齐后再 import 验证。
> 脚本与原始结果:`C:\work\robot\wheel_test\`(`run_tests2.ps1`、`results2.csv`、`deps2.csv`)。

**结论 1:14 个本地 wheel 全部可安装,补齐传递依赖后全部可 import,没有损坏的 wheel。**

**结论 2:按"能否去掉本地副本"分三类**

| 类别 | 包 | 说明 |
|---|---|---|
| **A. PyPI 钉死版本即有 win_arm64 wheel**(可直接不放进包) | `qai_appbuilder` 2.48.40(51MB)、`numpy` 2.3.1(10MB) | P1 通过,tag 为 `cp313-cp313-win_arm64`。`qai_appbuilder` 与 QAIRT 版本绑定,需版本对得上 |
| **B. 钉死版本无 wheel,但 PyPI 有更新版本的 win_arm64 wheel**(放开版本钉死后可用) | `aiohttp` 3.14.5、`httptools` 0.9.0、`cryptography` 46.0.3、`MarkupSafe` 3.0.4、`grimp` 3.17、`soundfile` 0.14.0 | P2 通过(`cryptography`、`MarkupSafe` 首轮网络超时,重测通过)。`cryptography` 46.x 在 `pyproject.toml` 允许的 `>=45,<47` 内。`soundfile` 只有 0.14.0 带 arm64 的 `libsndfile` DLL;钉死的 0.13.1 在 PyPI 只有 `py2-none-any`,装上后 import 失败 |
| **C. 必须自带本地 wheel** | `claude_agent_sdk`(73MB)、`opencv-python-headless`(25MB)、`kaldi_native_fbank`、`tiktoken`、`ripgrep`、`pyclipper` | PyPI 无任何版本的 win_arm64 wheel |

类别 C 的细节:
- `claude_agent_sdk`:本地 wheel 内含 **239MB 的 `claude.exe`**(`_bundled/`)。PyPI 上只有 `py3-none-any`(P3 装到 0.1.72,**不含 `claude.exe`**),所以不能替代。
- `opencv-python-headless`:sdist 构建失败(`No module named 'distutils'`)。
- `kaldi_native_fbank`:sdist 构建失败(`Failed to configure`)。
- `tiktoken`:sdist 构建失败,缺 Rust 编译器。
- `ripgrep`:本地 wheel 为 `rg.exe` 的打包;PyPI 无 win_arm64 wheel,sdist 构建在测试中因构建环境证书校验失败,**未能确认源码是否可构建**。
- `pyclipper`:**sdist 在有 VS 2022 的机器上构建成功**(`.pyd` 219KB,本地 wheel 214KB),但普通用户机器通常没有编译器,仍应视为需要本地 wheel。

**注意"能 P3 构建"不等于"无需本地 wheel"**:`aiohttp` 3.13.5 和 `httptools` 0.7.1 的 P3 也成功了,但 PyPI 上这两个版本**没有 win_arm64 wheel**(aiohttp 3.13.5 只有 win32 和 win_amd64),是在本机用 VS 2022 现场编译出来的。没有编译器的用户做不到。

**结论 3:对包体积的影响**
- 类别 A 可省:`qai_appbuilder` 51MB + `numpy` 10MB,合计约 60MB。
- 类别 B 合计仅约 6MB(aiohttp、cryptography、soundfile、grimp 等都是小包),**体积收益很小**;而且需放开版本钉死并**回归测试**(本次只验证了"能装、能 import",未验证业务功能,例如 MarkupSafe 3.x、aiohttp 3.14 是否影响现有代码)。建议保留在本地包中,不值得为 6MB 承担版本风险。
- 类别 C 约 101MB 仍必须自带,其中 `claude_agent_sdk` 73MB 占大头。

#### 3.1.3 wheel 的来源
仓库里没有构建记录,以下为根据 wheel 元数据的推断:

| 来源 | 依据 | 包 |
|---|---|---|
| 官方发布 | 作者 Qualcomm,README 写 GitHub Release | `aimet_onnx`、`aimet_torch` |
| 本项目构建 | 作者 quic-zhanweiw,主页为本仓库上游,PyPI 上也有同版本 | `qai_appbuilder` |
| 自行编译 | PyPI 无对应 wheel,构建工具版本较新:setuptools 82/83、maturin 1.9–1.14 | `aiohttp`、`cryptography`、`grimp`、`httptools`、`kaldi_native_fbank`、`MarkupSafe`、`opencv`、`pyclipper`、`ripgrep`、`tiktoken`、`clarabel` 等 |
| 手工重打包 | `Generator: manual-repack` | `soundfile`(把 arm64 的 libsndfile DLL 放进去) |
| 第三方 | README 写来自 cgohlke 的 ARM64 构建集 | `osqp`、`scs`、`cvxpy` |

- `claude_agent_sdk` 的 wheel 标签是 `py3-none-win_arm64`,PyPI 没有该标签,应为自行重打包(把平台的 `claude.exe` 放进 `_bundled/`),具体来源 **【待确认】**。
- 构建人、源码版本、构建命令、SHA256 均无记录。这批二进制会被所有 ARM64 用户安装,**建议改为 CI 构建并发布到 GitHub release,记录来源与哈希**。

#### 3.1.4 各架构是否需要
- **ARM64**:类别 C 必须;类别 A、B 可改走 PyPI(见上)。压缩包缺失时 Step 4a 被跳过,而 `cc-sdk` extra 依赖 `claude-agent-sdk`(类别 C),Step 4b2 很可能整体失败 **【推断,未实际在无 vendor 的环境跑 `Setup.bat` 验证】**。
- **x64**:`whl`(170MB)和 `openblas`(14MB)**完全不需要**,依据:`Setup.bat` Step 4a 在 x64 直接跳过;`setup_qairt_env.py` 第 910 行仅在 ARM64 安装本地 wheel(注释写明 x64 跳过);`qai_appbuilder` 在 x64 从 GitHub release 单独下载;`openblas` 与全部 wheel 均为 win_arm64。
- 其余三个目录对任意架构只是功能降级,不是硬依赖(代码读失败即返回 `None`,调用方降级):
  - `tiktoken` 缺失 → token 估算降级为 `len//2`。
  - `nltk_data` / `g2p_data` → Step 6 联网补齐,失败仅影响 TTS 首次推理。
- 脚本缺陷:`fetch_vendor_deps` 不区分架构,x64 也会下载整包。
- 以上 x64 结论均来自静态阅读代码,**未在 x64 机器上实际运行 `Setup.bat` 验证**。

#### 3.1.5 建议的拆分

| 新包 | 内容 | 谁需要 |
|---|---|---|
| `vendor-arm64-whl` | 类别 C 的 6 个 wheel(约 101MB)+ 类别 B 的小包(约 6MB,保留钉死版本以避免回归风险);类别 A 可改走 PyPI | 仅 ARM64 |
| `vendor-data` | `nltk_data`、`g2p_data`、`tiktoken` | 全部(可选组件) |
| `vendor-bin` | `guard`、`ast-grep`,按架构各一份 | 按架构只下一份,并去掉 Step 0c、0d 的重复下载 |
| (移除或另放) | AIMET 的 6 个 wheel + `openblas.dll` | 确认功能不再需要后移除 |

拆完后 x64 只需约 45MB 数据 + 一份 `bin\x64`(约 50MB);ARM64 可少下约 14MB 无用的 `openblas` 、另一个架构的 `bin`(约 100MB),以及类别 A 的约 60MB(若改走 PyPI)。AIMET 的 6 个 wheel 约 3.6MB,也可一并移除。

#### 3.1.6 本节测试的局限
- 只在一台 ARM64 机器、公司代理环境下测;**x64 未测**。
- 只验证了安装和 `import`,未跑业务功能。
- AIMET 的 6 个 wheel **未测**。
- PyPI 内容会变,结论有时效性(测试日期 2026-10-17)。
- 测试机有 VS 2022,sdist 构建结果不代表普通用户机器。

### 3.2 可延后/可选的默认项(候选)

| 组件 | 理由 | 建议 |
|---|---|---|
| Playwright Chromium(150MB) | 仅 `google_browser` 搜索和 `browser` 工具用 | 安装器中设为可选组件 |
| `search`、`cc-sdk` extra | 拖慢依赖解析 | 可选组件 |
| ast-grep(51MB) | 仅两个 agent 工具用 | 可选,或首次使用时下载 |
| Step 6 NLTK 预热 | 仅 melotts 英文路径用 | 与语音组件绑定 |
| `lark-oapi`、`wechatbot-sdk` | 不用 IM 渠道的用户不需要 | 评估改为 `channels` extra |
| Step 4c(pip 升级) | uv 环境下用处有限 | 可去掉 |
| Linux 的 `[dev,e2e]` | 终端用户不需要 | 默认不装 |
| Linux 的 QAIRT SDK | 只跑预构建模型时不需要 | 加开关,默认不装 |

---

## 4. 安装包方案

### 4.1 总体结构
安装器外壳(推荐 Inno Setup,备选 NSIS)只负责:界面、组件选择、网络设置、写配置、调用现有脚本。Python 环境创建、依赖安装仍由 `Setup.bat` / `setup.sh` 完成,避免重复实现。

### 4.2 发布形态

| 形态 | 内容 | 联网需求 | 适用 |
|---|---|---|---|
| 在线小包 | 脚本 + uv + aria2c | 需要(可配镜像/代理) | 网络正常 |
| 完整离线包(按架构分 arm64 / x64) | 上述 + vendor 数据 + Python 安装包 + wheelhouse + Chromium | 不需要 | 内网/弱网 |

离线包体积为粗略估计(约 700MB–1GB),**未实测**。

### 4.3 离线包建议内容
- vendor-deps 内容(`whl`、`nltk_data`、`g2p_data`、`tiktoken`)。
- `uv.exe`、`aria2c.exe`、`7zr.exe`、`ast-grep.exe`、`guard64.dll` 等。
- Python 3.13 安装包。
- 依赖 wheelhouse(提前下载好 `pyproject.toml` 解析出的全部依赖)。
- Playwright Chromium。
- 安装时使用 `--offline` / `--find-links` 安装。

### 4.4 限制与注意事项
- ARM64 与 x64 的 wheel、Python 不通用,需分别出包。
- `qai_appbuilder` 版本与 QAIRT SDK 版本强绑定(见 `scripts/qairt_release.json`),发版需同步。
- 部分 ARM64 wheel 为本地编译(如 grimp、clarabel),打包机建议使用 ARM64 机器。
- venv 含绝对路径,不能直接打包 venv。应打包 wheel,安装时再创建 venv。
- WDAC 环境下,安装包及内含 exe 需要代码签名。`Setup.bat` 已有 WDAC 回退逻辑,说明此场景真实存在。
- QAIRT SDK 与 VS Build Tools 体量大,不进默认包,作为"可选组件,在线下载"。
- 仓库中暂无现成安装器工程(未发现 `.iss` / `.nsi` / `.wxs`)。`Setup.bat` 注释提到的 `scripts/release/manifest.toml` 在当前目录树里未找到,**【待确认】**。

---

## 5. 网络配置(代理与镜像)

### 5.1 当前支持情况

| 环节 | 现状 |
|---|---|
| uv / PyPI | uv 读取 `HTTP(S)_PROXY`;镜像需 `UV_DEFAULT_INDEX`,脚本未设置 |
| uv 下载 Python | 需 `UV_PYTHON_INSTALL_MIRROR`,未处理 |
| aria2c | 包装脚本有 `-ProxyUrl`,仅语音模型步骤传入 |
| PowerShell 回退 | 默认走系统代理 |
| Playwright | 需 `PLAYWRIGHT_DOWNLOAD_HOST`,未处理 |
| NLTK | urllib 读取 `HTTPS_PROXY`,无镜像选项 |
| GitHub 资源 | 无镜像前缀机制 |
| Linux pip / npm | 靠环境变量;`node_bootstrap.sh` 仅在有代理时设 `NODE_USE_ENV_PROXY=1`;npm 使用 `envs/npmrc`,不读 `~/.npmrc` |

各变量对每一步是否真正生效,**【待确认:仅看过脚本,未实测】**。

### 5.2 设计
1. 安装向导增加"网络设置"页:
   - 代理地址、可选用户名/密码。
   - 镜像预设:官方 / 国内(清华、阿里、npmmirror 等)/ 自定义。
   - "测试连接":探测 github.com、pypi.org、cdn.playwright.dev、aidevhome.com 等。
2. 结果写入统一配置文件(暂定 `data/config/network.env`)。
3. `Setup.bat` / `setup.sh` 启动时读取并导出:

   | 变量 | 作用 |
   |---|---|
   | `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` | 通用代理 |
   | `UV_DEFAULT_INDEX` / `PIP_INDEX_URL` | PyPI 镜像 |
   | `UV_PYTHON_INSTALL_MIRROR` | Python 下载镜像 |
   | `PLAYWRIGHT_DOWNLOAD_HOST` | Chromium 镜像 |
   | `NPM_CONFIG_REGISTRY` | npm 镜像 |
   | `QAI_GITHUB_MIRROR`(新增) | GitHub 资源 URL 前缀 |

4. `download_with_aria2c.ps1` 默认从环境变量读取代理,而不是依赖调用方逐个传 `-ProxyUrl`。
5. GitHub 类 URL 统一经一个函数(如 `:resolve_url`)处理镜像前缀。
6. 命令行同样支持,如 `Setup.bat --proxy http://... --mirror cn`。
7. 代理密码不明文落盘:使用 DPAPI 加密,或仅保存在进程环境变量中。
8. 安装开头增加网络预检,不通时提前提示,而不是跑到最后才失败。

---

## 6. 安装器组件划分(草案)

| 组件 | 默认 | 说明 |
|---|---|---|
| 核心运行时(Python 环境 + 主依赖 + 数据初始化) | 必选 | 运行预构建模型 |
| vendor 依赖(ARM64 wheel 等) | 按架构自动 | ARM64 必选 |
| FileGuard(guard.zip) | 选中 | |
| ast-grep | 可选 | |
| 浏览器组件(Playwright + Chromium) | 可选 | |
| IM 渠道(飞书/微信 SDK) | 待定 | 取决于是否拆 extra |
| 语音/TTS 权重 | 可选 | |
| 前端构建工具链(Node + pnpm) | 可选 | 仅开发/自行构建前端 |
| PortableGit | 可选 | 仅 `exec shell='sh'` |
| 模型构建环境(QAIRT SDK + VS Build Tools) | 可选,在线下载 | 体量最大 |
| 桌面版 WebView2 | 自动判断 | |
| 开发工具链(`--dev`、Rust/tauri) | 不进安装器 | 仅开发者 |

---

## 7. Linux 方案(草案)

- 形态:`.tar.gz` + `install.sh`,或 `.deb`。不建议 AppImage / snap(NPU 依赖系统 `libcdsprpc`)。
- 与 `setup.sh` 的差距需补齐:
  - 默认不装 `[dev,e2e]`。
  - QAIRT SDK 加开关,默认不装。
  - 加重试与网络预检。
  - 确认浏览器工具、ast-grep 在 Linux 上的提供方式。**【待确认】**
- 离线包:`vendor/qairt/v*.zip` + pip wheelhouse,配合 `PIP_NO_INDEX=1`、`PIP_FIND_LINKS`。
- 前置依赖(Python 3.12、aarch64 的编译工具、FastRPC)仍需系统包管理器提供,安装器只检测并给出明确提示。

---

## 8. 推荐落地顺序

1. **改脚本(不依赖安装器,可马上缓解下载失败)**
   - 统一读取 `network.env`;包装脚本默认读代理变量。
   - 增加 `--mirror` / `--proxy`。
   - x64 跳过 vendor-deps 的 `whl`;或将其拆为 `vendor-whl-arm64` 与 `vendor-data` 两个包。
   - 增加网络预检。
   - 增加 `--no-browser` 等开关。
2. **在线小包**:Inno Setup 外壳 + 网络设置页 + 组件选择,内部调用 `Setup.bat --no-pause`。
3. **完整离线包**:加入 wheelhouse 与 Chromium,按架构各出一个。
4. **Linux 对齐**:补齐开关,出 tar.gz / deb。

---

## 9. 待讨论问题

1. 安装器技术选型:Inno Setup / NSIS / WiX(MSI)?是否需要 MSI 供企业部署?
2. 镜像预设列表由谁维护?是否内置一个默认的国内镜像集合?
3. `www.aidevhome.com` 的稳定性与归属;是否将 vendor-deps 迁移到 GitHub release 或内部制品库,并做多源回退?
4. `lark-oapi`、`wechatbot-sdk` 是否拆成可选 extra?
5. Playwright / ast-grep 是否改为首次使用时按需下载?
6. 是否需要代码签名证书,以及签名流程归谁负责?
7. 安装位置:现在 venv 在 `%LOCALAPPDATA%\QAIModelBuilder\envs`,数据在源码树 `data\`。安装包是否沿用这一布局,还是迁到 `Program Files` 与用户目录分离?
8. 升级与卸载:`Uninstall.bat` 现有逻辑如何与安装器的卸载程序对接?
9. QAIRT SDK 是否允许复用用户已有安装(`C:\Qualcomm\AIStack\QAIRT\<version>`)?
10. 离线包的更新节奏:随每个版本发布,还是只在依赖变化时发布?

---

## 附:涉及的主要文件
- `Setup.bat`、`Uninstall.bat`、`Start.bat`、`Build.bat`
- `setup.sh`、`start.sh`、`build.sh`
- `scripts/setup/download_with_aria2c.ps1`
- `scripts/setup/install_app_builder_deps.py`、`_pack_deps.py`
- `scripts/setup/predeploy_tts_runtime.py`
- `scripts/setup/setup_qairt_env.py`、`install_vs.ps1`、`ensure_signed_python.ps1`
- `scripts/setup/node_bootstrap.sh`
- `scripts/qairt_release.json`
- `vendor/whl/README.md`
- `pyproject.toml`

## uv / ast-grep from PyPI wheels (no GitHub needed)

`uv` and `ast-grep-cli` publish `win_arm64` / `win_amd64` wheels on PyPI whose only payload is the
executable under `<name>-<ver>.data/scripts/`. `setup_env.bat` now takes them from there first:

| Item | Source (first) | Fallback | Notes |
|---|---|---|---|
| uv | PyPI wheel `uv` (newest on the index) | GitHub release zip | tuna mirror only carries up to 0.9.9; PyPI has 0.13.x |
| ast-grep 0.45.1 | PyPI wheel `ast-grep-cli==0.45.1` | GitHub release zip | pinned; present on pypi.org and tuna |
| aria2c | GitHub release (via GitHub proxy when `cn`) | single-thread downloader | optional; no trustworthy PyPI build |
| guard.zip | GitHub release of this repo | skipped (FileGuard inactive) | the only item that still needs GitHub |

* Helper: `scripts/setup/fetch_pypi_exe.ps1`. Reads the PEP 503 simple index, picks the wheel, downloads it with
  `download_with_aria2c.ps1`, **verifies the SHA-256 published in the index** and only then extracts the exe.
  Index order follows `QAI_NET_PROFILE`: `official` = pypi.org then tuna, `cn` = tuna then pypi.org.
  A failing index (network, no matching wheel, SHA mismatch, exe missing) moves on to the next one.
* Never fatal: when every index fails the caller falls back to the old GitHub download.
* `QAI_NO_PYPI_EXE=1` skips the PyPI path (forces GitHub).
* Log lines are prefixed `[PYPI-EXE]`.