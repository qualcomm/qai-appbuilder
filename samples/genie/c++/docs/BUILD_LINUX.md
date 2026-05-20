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
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
cd ai-engine-direct-helper
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
`aarch64-oe-linux-gcc11.2` subdirectory under `lib/`):

```bash
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip
unzip v2.40.0.251030.zip
export QNN_SDK_ROOT=$(pwd)/v2.40.0.251030
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
./build_linux.sh --clean   # remove all build artefacts (including the
                           # in-source residue ExternalProject leaves in
                           # libsamplerate/, libcurl/, and the repo root)
./build_linux.sh --rebuild # equivalent to --clean followed by a fresh build
```

The script will:

1. Run CMake against `samples/genie/c++/Service/` with Linux-friendly flags.
2. Trigger an `ExternalProject` build of `libappbuilder.so` from the repo root
   (this is the same library that `qai_appbuilder` Python wheels link against).
3. Build `libsamplerate` from the bundled source.
4. Compile `GenieAPIService` and copy the QNN runtime libraries plus the
   default model config files into `Service/GenieService_v<VERSION>/`.

### Manual CMake invocation

If you want to drive the build yourself:

```bash
export QNN_SDK_ROOT=/absolute/path/to/v2.40.0.251030

cd samples/genie/c++/Service
mkdir -p build_linux && cd build_linux

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DQNN_STUB_VERSION=v73 \
    -DQNN_PLATFORM=aarch64-oe-linux-gcc11.2

cmake --build . --parallel $(nproc)
```

The final artefacts will be placed in
`samples/genie/c++/Service/GenieService_v<VERSION>/`:

```
GenieAPIService               # the executable
libappbuilder.so              # built from the top-level src/
libGenie.so / libQnnHtp.so /  # copied from QNN_SDK_ROOT
libQnnSystem.so / ...
config/                       # default model configs
```

---

## 5. Build options

`build_linux.sh` reads the **same environment variables that QAI AppBuilder
uses** (so the values you already export for `python -m build -w` work here
without any change):

| Env var          | Default                       | Meaning |
|------------------|-------------------------------|---------|
| `QNN_SDK_ROOT`   | *(required)*                  | Path to the extracted QAIRT SDK. |
| `QAI_TOOLCHAINS` | `aarch64-oe-linux-gcc11.2`    | Toolchain subdirectory under `$QNN_SDK_ROOT/lib/`. The same value the `python -m build -w` flow uses. Common Linux value: `aarch64-oe-linux-gcc11.2`. |
| `QAI_HEXAGONARCH`| `73`                          | Hexagon DSP arch number (just the number, e.g. `68` / `73` / `81`). The script automatically maps this to `v${QAI_HEXAGONARCH}` for the QNN library names. |

Example (matches `setup.py`'s example block):

```bash
export QNN_SDK_ROOT=/abs/path/to/v2.40.0.251030
export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2
export QAI_HEXAGONARCH=73
./build_linux.sh
```

### Lower-level overrides

These are only useful if you want to bypass the `QAI_*` resolution logic
entirely (e.g. when the SDK uses an unusual subdir name):

| Variable           | Default                           | Meaning |
|--------------------|-----------------------------------|---------|
| `QNN_PLATFORM`     | derived from `QAI_TOOLCHAINS`     | Direct override of the lib subdir (`aarch64-oe-linux-gcc11.2`). |
| `QNN_STUB_VERSION` | derived from `QAI_HEXAGONARCH`    | Direct override of the Hexagon stub version string (`v73`). |

### Other CMake / build switches

| Variable           | Default                           | Meaning |
|--------------------|-----------------------------------|---------|
| `BUILD_TYPE`       | `Release`                         | Standard CMake build type. |
| `JOBS`             | `$(nproc)`                        | Parallel jobs for the build. |
| `USE_MNN`          | `OFF`                             | Enable MNN backend. Not validated on Linux yet. |
| `USE_GGUF`         | `OFF`                             | Enable llama.cpp / GGUF backend. Not validated on Linux yet. |
| `BUILD_AS_DLL`     | `OFF`                             | Build `libGenieAPILibrary.so` instead of the executable. |
| `BUILD_LINUX_CLIENT` | `OFF`                           | Build the `GenieAPIClient` sample (requires libcurl + OpenSSL system packages: `sudo apt install libssl-dev`). |

---

## 6. Run

Set the runtime library paths and start the service:

```bash
export QNN_SDK_ROOT=/absolute/path/to/v2.40.0.251030

# Both the QAIRT runtime and our build dir need to be on LD_LIBRARY_PATH.
OUT_DIR=$(pwd)/samples/genie/c++/Service/GenieService_v2.1.4
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2:$OUT_DIR:$LD_LIBRARY_PATH

# Tell the Hexagon DSP loader where its skel files live.
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned

cd $OUT_DIR
./GenieAPIService -c config/<your_model>/config.json -l -p 8910
```

The OpenAI-compatible API is then available at `http://<host>:8910/v1/...`.
See [API.md](API.md) for the endpoint reference and
[USAGE.MD](USAGE.MD) for client samples.

---

## 6.1. Deploying to a target device — what you must edit

The CMake step copies the entire `samples/genie/python/models/` tree into
`<output>/config/` so you have all the bundled per-model configs ready to
ship. Two things in that copy are **authored for the Windows reference
build** and you almost always need to adjust them for your Linux target.

### 6.1.1. `config/htp_backend_ext_config.json` — already auto-fixed

The shipped Windows version contains:
```json
{ "devices": [ { "soc_id": 60, "dsp_arch": "v73", ... } ] }
```
That `soc_id=60 / v73` combination is specific to Snapdragon X-Elite class
laptops and is wrong for any other SoC.

**The Linux build script regenerates this file for you** in
`<output>/config/htp_backend_ext_config.json` based on `QAI_HEXAGONARCH`
(see `scripts/post_build_linux.sh`). The auto-generated file:
- writes only `dsp_arch` (e.g. `"v73"`, `"v75"`, `"v79"`, `"v81"`),
- omits `soc_id` so the QNN runtime auto-detects it.

If your target SoC requires an explicit `soc_id`, edit the file post-deploy
and add it back:
```json
{ "devices": [ { "soc_id": <your_soc_id>, "dsp_arch": "v73", "cores": [...] } ] }
```
Refer to the Qualcomm Snapdragon SoC IDs documentation for the right value.

### 6.1.2. `config/<model>/config.json` — you have to edit at deploy time

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
You can also use paths that are **relative to the working directory in which
you run `./GenieAPIService`** — both styles work as long as the runtime can
resolve them.

A one-liner that converts all backslashes to forward slashes if the rest
of the layout already matches:
```bash
sed -i 's#\\\\#/#g' /path/to/your/<model>/config.json
```

### 6.1.3. Quick deployment checklist

1. `tar` (or `rsync`) the `<output>/` directory to the device.
2. Upload your model bin / tokenizer files separately (they are large and
   not part of the source tree).
3. Edit `config/<model>/config.json` — fix `tokenizer.path`, `ctx-bins`,
   and `backend.extensions` to match the actual on-device locations.
4. (Optional) Edit `config/htp_backend_ext_config.json` to add `soc_id` if
   your platform needs it.
5. Run `./GenieAPIService -c config/<model>/config.json -l -p 8910`.

---

## 7. Troubleshooting

**`error while loading shared libraries: libGenie.so`**
You forgot `LD_LIBRARY_PATH` (see step 6) or `$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2/`
does not contain `libGenie.so` (wrong SDK or wrong `QNN_PLATFORM`).

**`Unable to find a valid interface` / `Error initializing QNN Function Pointers`**
Usually a runtime version mismatch between `libappbuilder.so` and the QAIRT
SDK. Re-build with the SDK version that you intend to deploy and make sure
`QNN_SDK_ROOT` is exported during the build *and* during runtime.

**`undefined reference to pthread_*` or `dlopen`**
Ensure you used `build_linux.sh` (or pass `-DCMAKE_BUILD_TYPE=Release` with the
updated CMake files); the new Linux branch links `pthread` and `dl`.

**Hexagon skel file missing (`libQnnHtpV{XX}Skel.so`)**
Check that `QNN_STUB_VERSION` matches the SoC variant your device uses; then
verify the file exists under `$QNN_SDK_ROOT/lib/hexagon-v{XX}/unsigned/`.

**Compilation fails with `_CountOneBits64 was not declared`**
This only happens on the MSVC ARM64 Windows path. The Linux build does not use
`platform_fix.h` and is unaffected.

---

## 8. What was changed for Linux support — and how Windows / Android are protected

### 8.1 Compatibility principle

Every change in this port is **strictly fenced off** from the existing Windows
and Android build paths so that a Linux merge cannot regress those targets.
Two complementary mechanisms are used:

1. **CMake guards** — every new code path is gated by
   `if (UNIX AND NOT ANDROID)` (or the more explicit `CMAKE_SYSTEM_NAME
   STREQUAL "Linux"` where appropriate). Note that the **Android build is
   driven by `ndk-build` + `Application.mk`**, **not** by these CMakeLists at
   all (see `Service/Makefile`), so any CMake change is structurally invisible
   to Android.
2. **C/C++ preprocessor guards** — every newly added `#include` or
   compatibility shim is wrapped in
   `#if defined(__linux__) && !defined(__ANDROID__) … #endif`.
   This keeps the include set and the macro set on Windows (MSVC) and Android
   (NDK clang) byte-identical to what they were before.

The single source-level change that does not need a `#if` guard is the fix in
`model/def.h` — removing the `extra qualification not allowed` form
`struct std::hash<…>` inside `namespace std`. The new form is plain standard
C++, accepted by MSVC, gcc and clang alike, so it improves portability without
behaviour change.

### 8.2 Reference list of edited files

The Linux port is intentionally minimal. Reference list of edited files:

| File | Change |
|---|---|
| `samples/genie/c++/Service/CMakeLists.txt` | Allow Linux/Android in addition to Windows; add UNIX compile/link options; gate `examples/GenieAPIClient` behind `BUILD_LINUX_CLIENT=ON`. |
| `samples/genie/c++/Service/src/GenieAPIService/CMakeLists.txt` | Add UNIX (`add_executable`) branch; gate ARM64 MSVC platform fix to MSVC only; set `$ORIGIN` rpath. |
| `samples/genie/c++/Service/src/GenieAPIService/dependents.cmake` | Add Linux QNN platform / library naming, build `libappbuilder.so` and `libsamplerate` via `ExternalProject`. |
| `samples/genie/c++/Service/src/common/log.h` | ANSI color macros now also defined on Linux. |
| `samples/genie/c++/Service/src/common/utils.h` | Add explicit `<atomic>`, `<chrono>`, `<algorithm>`, `<cstdint>`, ... headers (gcc 13 dropped many transitive includes that MSVC still ships). |
| `samples/genie/c++/Service/src/common/utils.cpp` | `isPortAvailable` implemented with POSIX sockets. |
| `samples/genie/c++/Service/src/GenieAPIService/src/config.h` | Include `<unistd.h>` on non-Windows for `getcwd`. |
| `samples/genie/c++/Service/src/GenieAPIService/src/model/def.h` | Add `<cstdint>`; remove redundant `std::` qualifier inside `namespace std` (rejected by gcc). |
| `samples/genie/c++/Service/src/GenieAPIService/src/model/model_manager.h` | Add `<atomic>`. |
| `samples/genie/c++/Service/src/GenieAPIService/src/context/qnn/genie.h` | Add `<condition_variable>`. |
| `samples/genie/c++/Service/src/GenieAPIService/src/context/torch_helper/base.h` | Add `<algorithm>` / `<cmath>` / `<cstdint>` / `<iterator>` / `<limits>` / `<stdexcept>`. |
| `samples/genie/c++/Service/src/GenieAPIService/src/context/qnn/qwen2_5_omini/qwen_2_5_omini.cpp` | Provide `std::sqrtf` shim before including LibrosaCpp on non-MSVC compilers. |
| `samples/genie/c++/Service/examples/GenieAPIClient/CMakeLists.txt` | Linux link rules (libcurl + pthread + dl). |
| `samples/genie/c++/build_linux.sh` (new) | One-click build script. |
| `samples/genie/c++/scripts/post_build_linux.sh` (new) | Generates a Linux-correct `config/htp_backend_ext_config.json` (right `dsp_arch`, no Windows-specific `soc_id`). Per-model `config.json` files are deliberately left untouched. |
| `samples/genie/c++/docs/BUILD_LINUX.md` (new) | This document. |

No business logic was changed.
