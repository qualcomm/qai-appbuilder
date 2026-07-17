# GenieAPIService – Build for Linux (ARM64)

This document explains how to build the **GenieAPIService** C++ binary on a
Qualcomm-based ARM64 Linux device (or in any aarch64 Linux environment that
matches the QAIRT runtime ABI).

> The build environment is **identical to QAI AppBuilder's Linux build
> environment**. If you can already build the `qai_appbuilder` wheel on your
> machine, you can build GenieAPIService on the same machine with no extra
> dependencies.

---

## 1. Prerequisites

| Item            | Recommended version |
|-----------------|---------------------|
| OS              | Ubuntu 22.04 LTS aarch64 (or any glibc ≥ 2.31 distro) |
| Compiler        | gcc / g++ ≥ 11 (C++17) |
| CMake           | ≥ 3.18 |
| QAIRT SDK       | 2.40.0+ (must contain `lib/aarch64-oe-linux-gcc11.2/`) |
| git             | any recent version |

Install the system packages:

```bash
sudo apt update
sudo apt install -y git cmake build-essential
```

---

## 2. Clone the repository

```bash
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder
```

If you already cloned it without `--recursive`, **you must** initialise the
submodules (the Linux build needs `External/CLI11`, `External/cpp-httplib`,
`External/json`, `External/libsamplerate`, `External/dr_libs`,
`External/stb`, `External/LibrosaCpp`):

```bash
git submodule update --init --recursive
```

Verify that the CLI11 headers are present:

```bash
ls samples/genie/c++/External/CLI11/include/CLI/CLI.hpp \
   || ls samples/genie/c++/External/cli11/include/CLI/CLI.hpp
```

If neither path is populated, the CMake configure step will abort with a
`CLI/CLI.hpp not found` message.

---

## 3. Get the QAIRT SDK

Download a QAIRT SDK that ships the Linux ARM64 runtime (look for the
`aarch64-oe-linux-gcc11.2` subdirectory under `lib/`). The example below uses
`2.44.0.260225`, the version currently validated for this project — substitute
whatever QAIRT SDK version you actually have:

```bash
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.44.0.260225/v2.44.0.260225.zip
unzip v2.44.0.260225.zip
export QNN_SDK_ROOT=$(pwd)/qairt/2.44.0.260225
```

Verify the layout:

```bash
ls $QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2/libGenie.so
ls $QNN_SDK_ROOT/include/Genie/GenieDialog.h
```

---

## 4. Build

The simplest path is the helper script:

```bash
cd samples/genie/c++
chmod +x build_linux.sh
./build_linux.sh           # configure & build
```

`build_linux.sh` only knows one mode — configure then build. It does **not**
support `--clean`/`--rebuild` flags; to do a clean build, remove the
`Service/build-linux/` directory yourself before re-running the script.

The script will:

1. Run `cmake -S Service -B Service/build-linux` with the Linux-friendly flags
   described below (this is a plain CMake configure/build invocation — the
   heavy lifting for `libappbuilder.so`/`libsamplerate` is done by
   `ExternalProject` targets declared inside `src/GenieAPIService/platform.cmake`,
   not by the shell script itself).
2. Build `GenieAPIService` (and, transitively, `libappbuilder.so`,
   `libsamplerate`, and — only on Linux — an mbedTLS copy fetched from
   `github.com/Mbed-TLS/mbedtls` at build time for the HTTPS transport used
   by `cpp-httplib`).
3. Copy the default model configs and metadata files (`service_config.json`,
   `sensitive_keywords.json`, `test_service.py`, the build `version` file)
   into the output directory below.

### Output

The final artefacts are placed in a fixed, version-free, platform-specific
directory (set by `BUILD_PATH` in `Service/CMakeLists.txt`):
`samples/genie/c++/Service/build-linux/GenieService-linux-arm64/`.

```
Service/build-linux/GenieService-linux-arm64/
├── GenieAPIService               # the executable
└── libappbuilder.so              # built from the top-level src/
```

> **Note:** The Linux output is intentionally minimal — just the binaries
> you need to deploy to a target board that already has the QAIRT SDK
> installed. The QNN runtime libs (`libGenie.so`, `libQnnHtp.so`, etc.) are
> NOT copied because they already exist on the target device as part of the
> SDK installation.
>
> On Windows the output directory is `GenieService-win-arm64` (no
> `build-linux/` layer) and includes the full SDK runtime for standalone
> deployment. Switching QAIRT SDK versions and rebuilding overwrites the
> previous artefacts in place — the directory name never encodes the SDK
> version.

> **Linux is QNN/Genie-only.** Unlike Windows/MSVC, the Linux build does
> **not** support the `USE_MNN`/`USE_GGUF` backends. `Service/CMakeLists.txt`
> actively rejects them on non-MSVC platforms — passing `-DUSE_MNN=ON` or
> `-DUSE_GGUF=ON` makes the CMake **configure step fail immediately** with
> `FATAL_ERROR "USE_MNN/USE_GGUF is supported only on Windows/MSVC; Linux
> builds are QNN-only."` There is no way to get a Linux build that also
> loads MNN or GGUF models.

### Build options

`build_linux.sh` only reads the environment variables it explicitly
documents at the top of the script — it does **not** forward arbitrary
`-D...` flags, and it does **not** read `USE_MNN`/`USE_GGUF` (both have no
effect if exported before running the script):

| Variable           | Default                        | Meaning |
|--------------------|---------------------------------|---------|
| `QNN_SDK_ROOT`     | *(required)*                    | Path to the extracted QAIRT SDK. The script aborts immediately if this is unset. |
| `QNN_STUB_VERSION` | `v73`                           | Hexagon DSP stub version used at runtime setup. |
| `QNN_PLATFORM`     | `aarch64-oe-linux-gcc11.2`      | QNN toolchain/platform string, forwarded to CMake as `-DQNN_PLATFORM=...`. |
| `BUILD_TYPE`       | `Release`                      | Standard CMake build type. |
| `JOBS`             | `$(nproc)`                     | Parallel jobs passed to `cmake --build ... -j`. |
| `BUILD_AS_DLL`     | `OFF`                          | `OFF` builds the `GenieAPIService` executable; `ON` builds only `libGenieAPILibrary.so` instead. |

`GenieAPIClient`, the CLI sample, always builds alongside `GenieAPIService`
— there is no option to disable it. It links against a minimal static
`libcurl` (HTTP only, no TLS/LDAP/SSH2) that its own
`examples/GenieAPIClient/CMakeLists.txt` builds from `External/curl/` via
`ExternalProject`, so no separate curl installation is needed.

---

## 5. Run

Set the runtime library paths and start the service:

```bash
export QNN_SDK_ROOT=/absolute/path/to/2.44.0.260225

# Fixed output directory name (see section 4 "Output" above).
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

The OpenAI-compatible API is then available at `http://<host>:8910/v1/...`.
See [API.md](API.md) for the endpoint reference and
[USAGE.MD](USAGE.MD) for client samples.

---

### 5.1. Deploying to a target device

Copy **all files** in the build output directory
(`build-linux/GenieService-linux-arm64/`) to the target device.
The build only produces runtime-essential binaries; every file in that
directory is needed.

You also need to prepare model config files and the HTP backend extension
config. Both require path edits before the service can start.

#### `config/<model>/config.json` — you have to edit at deploy time

Every per-model config bundled by Qualcomm uses **Windows backslash paths**
that point to the developer's source tree, e.g.:
```json
"tokenizer": { "path": "genie\\python\\models\\Phi-3.5-mini\\tokenizer.json" }
"binary":    { "ctx-bins": [ "genie\\python\\models\\Phi-3.5-mini\\weight_sharing_model_1_of_4.serialized.bin", ... ] }
"backend":   { "extensions": "genie\\python\\config\\htp_backend_ext_config.json" }
```
On Linux you need every reference to use **forward slashes** AND point at
the actual path on the target device where you uploaded the model files.
The build script does **NOT** rewrite these paths because their correct
target depends on where you copy the package on the device.

A typical post-deploy fix-up looks like this. Suppose you uploaded the
package to `/data/genie/` and the model files to
`/data/genie/models/Phi-3.5-mini/`:
```json
"tokenizer": { "path": "/data/genie/models/Phi-3.5-mini/tokenizer.json" }
"binary":    { "ctx-bins": [
    "/data/genie/models/Phi-3.5-mini/weight_sharing_model_1_of_4.serialized.bin",
    "/data/genie/models/Phi-3.5-mini/weight_sharing_model_2_of_4.serialized.bin",
    ...
] }
"backend":   { "extensions": "/data/genie/config/htp_backend_ext_config.json" }
```

#### `htp_backend_ext_config.json` — also needs path edits

The HTP backend extension config (`htp_backend_ext_config.json`) referenced
by the model config indicates the HTP version to target. Set `dsp_arch` to
the correct value, for example:

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


#### Quick deployment checklist

1. Copy **all files** from the build output directory to the target device
   (e.g. via `tar`, `rsync`, or `scp`).
2. Upload your model bin / tokenizer files separately (they are large and
   not part of the source tree).
3. Edit `config/<model>/config.json` — fix `tokenizer.path`, `ctx-bins`,
   and `backend.extensions` to match the actual on-device locations.
4. Edit `htp_backend_ext_config.json` if it contains hard-coded paths that
   don't resolve on the target device.
5. Run `./GenieAPIService -c config/<model>/config.json -l -p 8910`.

---

## 6. Troubleshooting

**`error while loading shared libraries: libGenie.so`**
You forgot `LD_LIBRARY_PATH` (see step 5) or `$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2/`
does not contain `libGenie.so` (wrong SDK or wrong `QNN_PLATFORM`).

**`Unable to find a valid interface` / `Error initializing QNN Function Pointers`**
Usually a runtime version mismatch between `libappbuilder.so` and the QAIRT
SDK. Re-build with the SDK version that you intend to deploy and make sure
`QNN_SDK_ROOT` is exported during the build *and* during runtime.

**`undefined reference to pthread_*` or `dlopen`**
Ensure you used `build_linux.sh` (or pass `-DCMAKE_BUILD_TYPE=Release` with the
updated CMake files); the new Linux branch links `pthread` and `dl`.

**Hexagon skel file missing (`libQnnHtpV{XX}Skel.so`)**
Verify the file exists under `$QNN_SDK_ROOT/lib/hexagon-vXX/unsigned/`
(where `XX` matches your SoC's DSP arch, e.g. `73`, `75`).

**`CLI11 header (CLI/CLI.hpp) not found ...` during CMake configure**
The git submodule for CLI11 was not initialised. Run:
```bash
git submodule update --init --recursive
```
`build_linux.sh` itself does **not** pre-check this; the error surfaces from
the CMake configure step, so run the command above proactively if you cloned
without `--recursive`.
