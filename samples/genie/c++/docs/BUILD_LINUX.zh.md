# GenieAPIService – 面向 Linux (ARM64) 的构建说明

本文档说明如何在基于 Qualcomm 的 ARM64 Linux 设备上（或在与 QAIRT 运行时 ABI 匹配的任意
aarch64 Linux 环境中）构建 **GenieAPIService** C++ 二进制文件。

> 该构建环境**与 QAI AppBuilder 的 Linux 构建环境完全相同**。如果你的机器已经能够构建
> `qai_appbuilder` wheel，那么在同一台机器上构建 GenieAPIService 不需要任何额外依赖。

---

## 1. 前置条件

| Item            | Recommended version |
|-----------------|---------------------|
| OS              | Ubuntu 22.04 LTS aarch64 (or any glibc ≥ 2.31 distro) |
| Compiler        | gcc / g++ ≥ 11 (C++17) |
| CMake           | ≥ 3.18 |
| QAIRT SDK       | 2.40.0+ (must contain `lib/aarch64-oe-linux-gcc11.2/`) |
| git             | any recent version |

安装系统软件包：

```bash
sudo apt update
sudo apt install -y git cmake build-essential
```

---

## 2. 克隆仓库

```bash
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder
```

如果你之前克隆时没有加 `--recursive`，**必须**初始化子模块（Linux 构建需要
`External/CLI11`、`External/cpp-httplib`、`External/json`、`External/libsamplerate`、
`External/dr_libs`、`External/stb`、`External/LibrosaCpp`）：

```bash
git submodule update --init --recursive
```

验证 CLI11 头文件是否存在：

```bash
ls samples/genie/c++/External/CLI11/include/CLI/CLI.hpp \
   || ls samples/genie/c++/External/cli11/include/CLI/CLI.hpp
```

如果两个路径都不存在，CMake 配置步骤会因 `CLI/CLI.hpp not found` 消息而中止。

---

## 3. 获取 QAIRT SDK

下载一份包含 Linux ARM64 运行时的 QAIRT SDK（在 `lib/` 下查找
`aarch64-oe-linux-gcc11.2` 子目录）。下面的示例使用的是 `2.44.0.260225`，即本项目当前已验证的版本
——请根据你实际拥有的 QAIRT SDK 版本进行替换：

```bash
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.44.0.260225/v2.44.0.260225.zip
unzip v2.44.0.260225.zip
export QNN_SDK_ROOT=$(pwd)/qairt/2.44.0.260225
```

验证目录结构：

```bash
ls $QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2/libGenie.so
ls $QNN_SDK_ROOT/include/Genie/GenieDialog.h
```

---

## 4. 构建

最简单的方式是使用辅助脚本：

```bash
cd samples/genie/c++
chmod +x build_linux.sh
./build_linux.sh           # configure & build
```

`build_linux.sh` 只有一种模式——先配置再构建。它**不**支持 `--clean`/`--rebuild`
参数；如果要做一次干净构建，需要在重新运行脚本之前自行删除
`Service/build-linux/` 目录。

该脚本会：

1. 使用下文所述的 Linux 友好参数运行 `cmake -S Service -B Service/build-linux`
   （这只是一次普通的 CMake 配置/构建调用——`libappbuilder.so`/`libsamplerate`
   的实际构建工作是由 `src/GenieAPIService/platform.cmake` 中声明的
   `ExternalProject` 目标完成的，而不是这个 shell 脚本本身）。
2. 构建 `GenieAPIService`（并传递性地构建 `libappbuilder.so`、`libsamplerate`，
   以及——仅在 Linux 上——为 `cpp-httplib` 使用的 HTTPS 传输层在构建期从
   `github.com/Mbed-TLS/mbedtls` 拉取的一份 mbedTLS 副本）。
3. 将默认模型配置文件与元数据文件（`service_config.json`、`sensitive_keywords.json`、
   `test_service.py`、构建版本文件 `version`）复制到下面所述的输出目录中。

### 输出

最终的构建产物会放置于一个固定、不带版本号、按平台区分的目录（由
`Service/CMakeLists.txt` 中的 `BUILD_PATH` 设定）：
`samples/genie/c++/Service/build-linux/GenieService-linux-arm64/`。

```
Service/build-linux/GenieService-linux-arm64/
├── GenieAPIService               # the executable
└── libappbuilder.so              # built from the top-level src/
```

> **注意：** Linux 端的输出内容特意保持精简——只包含你需要部署到目标板卡上的
> 二进制文件，目标板卡上应已安装 QAIRT SDK。QNN 运行时库
> （`libGenie.so`、`libQnnHtp.so` 等）不会被复制，因为它们作为 SDK 安装的一部分，
> 已经存在于目标设备上。
>
> 在 Windows 上，输出目录是 `GenieService-win-arm64`（没有
> `build-linux/` 这一层）并包含完整的 SDK 运行时，以便独立部署。切换 QAIRT SDK
> 版本后重新构建会直接覆盖之前的产物——目录名从不包含 SDK 版本号。

> **Linux 端仅支持 QNN/Genie 后端。** 与 Windows/MSVC 不同，Linux 构建
> **不**支持 `USE_MNN`/`USE_GGUF` 后端。`Service/CMakeLists.txt`
> 在非 MSVC 平台上会主动拒绝这两个选项——传入 `-DUSE_MNN=ON` 或
> `-DUSE_GGUF=ON` 都会使 CMake **配置步骤立即失败**，并报出
> `FATAL_ERROR "USE_MNN/USE_GGUF is supported only on
> Windows/MSVC; Linux builds are QNN-only."`。目前没有任何方法能得到一份
> 同时支持加载 MNN 或 GGUF 模型的 Linux 构建。

### 构建选项

`build_linux.sh` 只读取脚本开头明确列出的环境变量——它**不会**转发任意的
`-D...` 参数，也**不会**读取 `USE_MNN`/`USE_GGUF`
（这两个即使在运行脚本之前导出了，也不会产生任何效果）：

| Variable           | Default                        | Meaning |
|--------------------|---------------------------------|---------|
| `QNN_SDK_ROOT`     | *(required)*                    | Path to the extracted QAIRT SDK. The script aborts immediately if this is unset. |
| `QNN_STUB_VERSION` | `v73`                           | Hexagon DSP stub version used at runtime setup. |
| `QNN_PLATFORM`     | `aarch64-oe-linux-gcc11.2`      | QNN toolchain/platform string, forwarded to CMake as `-DQNN_PLATFORM=...`. |
| `BUILD_TYPE`       | `Release`                      | Standard CMake build type. |
| `JOBS`             | `$(nproc)`                     | Parallel jobs passed to `cmake --build ... -j`. |
| `BUILD_AS_DLL`     | `OFF`                          | `OFF` builds the `GenieAPIService` executable; `ON` builds only `libGenieAPILibrary.so` instead. |

`GenieAPIClient` CLI 示例程序始终随 `GenieAPIService` 一起构建，没有开关能关闭它。
它链接的是一份仅支持 HTTP（不含 TLS/LDAP/SSH2）的静态 `libcurl`，由它自己的
`examples/GenieAPIClient/CMakeLists.txt` 通过 `ExternalProject` 从 `External/curl/`
源码自动构建，无需单独安装 curl。

---

## 5. 运行

设置运行时库路径并启动服务：

```bash
export QNN_SDK_ROOT=/absolute/path/to/2.44.0.260225

# 固定的输出目录名（见上方第 4 节“输出”）。
OUT_DIR=$(pwd)/samples/genie/c++/Service/build-linux/GenieService-linux-arm64

# Both the QAIRT runtime and our build dir need to be on LD_LIBRARY_PATH.
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2:$OUT_DIR:$LD_LIBRARY_PATH

# Tell the Hexagon DSP loader where its skel files live (adjust vXX to match
# your SoC, e.g. v73, v75). You can find the actual value with:
#     ls $QNN_SDK_ROOT/lib/hexagon-*
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-vXX/unsigned

cd $OUT_DIR
./GenieAPIService -c config/<your_model>/config.json -l -p 8910
```

之后即可通过 `http://<host>:8910/v1/...` 访问兼容 OpenAI 的 API。
端点参考请参见 [API.zh.md](API.zh.md)，客户端示例请参见
[USAGE.zh.MD](USAGE.zh.MD)。

---

### 5.1. 部署到目标设备

将构建输出目录（`build-linux/GenieService-linux-arm64/`）中的
**全部文件**复制到目标设备。该构建只生成运行时必需的二进制文件，
该目录下的每一个文件都是必需的。

你还需要准备模型配置文件和 HTP backend 扩展配置。这两者在服务启动前都需要修改路径。

#### `config/<model>/config.json` —— 部署时你需要自行编辑

Qualcomm 打包的每个逐模型配置文件都使用**指向开发者源码树的 Windows 反斜杠路径**，例如：
```json
"tokenizer": { "path": "genie\\python\\models\\Phi-3.5-mini\\tokenizer.json" }
"binary":    { "ctx-bins": [ "genie\\python\\models\\Phi-3.5-mini\\weight_sharing_model_1_of_4.serialized.bin", ... ] }
"backend":   { "extensions": "genie\\python\\config\\htp_backend_ext_config.json" }
```
在 Linux 上，你需要把所有引用都改为使用**正斜杠**，并且**指向**你上传模型文件后
目标设备上的**实际路径**。构建脚本**不会**重写这些路径，因为它们正确的目标位置
取决于你把该软件包复制到设备上的哪个位置。

一个典型的部署后修正示例如下所示。假设你把该软件包上传到了
`/data/genie/`，模型文件上传到了
`/data/genie/models/Phi-3.5-mini/`：
```json
"tokenizer": { "path": "/data/genie/models/Phi-3.5-mini/tokenizer.json" }
"binary":    { "ctx-bins": [
    "/data/genie/models/Phi-3.5-mini/weight_sharing_model_1_of_4.serialized.bin",
    "/data/genie/models/Phi-3.5-mini/weight_sharing_model_2_of_4.serialized.bin",
    ...
] }
"backend":   { "extensions": "/data/genie/config/htp_backend_ext_config.json" }
```

#### `htp_backend_ext_config.json` —— 同样需要修改路径

模型配置中引用的 HTP backend 扩展配置（`htp_backend_ext_config.json`）
用于指示 HTP 版本。请将 `dsp_arch` 设置为正确的值。例如：

```json
{
  "devices": [
    {
      "dsp_arch": "v73",
      "cores":[{
        "perf_profile": "burst",
        "rpc_control_latency": 100
      }]
    }
  ]
}

```


#### 快速部署检查清单

1. 将构建输出目录中的**全部文件**复制到目标设备
   （例如通过 `tar`、`rsync` 或 `scp`）。
2. 单独上传你的模型权重文件/分词器文件（这些文件体积较大，不属于源码树的一部分）。
3. 编辑 `config/<model>/config.json`——修正 `tokenizer.path`、`ctx-bins`
   以及 `backend.extensions`，使其与设备上实际的存放位置一致。
4. 如果 `htp_backend_ext_config.json` 中包含在目标设备上无法解析的硬编码路径，
   也要进行编辑。
5. 运行 `./GenieAPIService -c config/<model>/config.json -l -p 8910`。

---

## 6. 故障排查

**`error while loading shared libraries: libGenie.so`**
你忘记设置 `LD_LIBRARY_PATH`（见第 5 步），或者
`$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2/` 中不包含 `libGenie.so`
（SDK 版本不对，或 `QNN_PLATFORM` 不对）。

**`Unable to find a valid interface` / `Error initializing QNN Function Pointers`**
通常是 `libappbuilder.so` 与 QAIRT SDK 之间的运行时版本不匹配。
请使用你打算部署的那个 SDK 版本重新构建，并确保在构建期间
*和*运行期间都导出了 `QNN_SDK_ROOT`。

**`undefined reference to pthread_*` or `dlopen`**
请确保你使用的是 `build_linux.sh`（或者传入
`-DCMAKE_BUILD_TYPE=Release` 并使用更新后的 CMake 文件）；新的 Linux
分支会链接 `pthread` 和 `dl`。

**Hexagon skel 文件缺失（`libQnnHtpV{XX}Skel.so`）**
请确认该文件存在于 `$QNN_SDK_ROOT/lib/hexagon-vXX/unsigned/` 下
（其中 `XX` 要与你 SoC 的 DSP 架构一致，例如 `73`、`75`）。

**CMake 配置期间出现 `CLI11 header (CLI/CLI.hpp) not found ...`**
CLI11 的 git 子模块没有被初始化。运行：
```bash
git submodule update --init --recursive
```
`build_linux.sh` 本身**不会**预先检查这一点；该错误是从 CMake 配置步骤中
冒出来的，因此如果你克隆时没有加 `--recursive`，请主动提前运行上述命令。
