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

If you already cloned it without `--recursive`:

```bash
git submodule update --init --recursive
```

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
./build_linux.sh
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

All options are CMake cache variables; pass them with `-D<name>=<value>` or
via environment variables to `build_linux.sh`:

| Variable           | Default                           | Meaning |
|--------------------|-----------------------------------|---------|
| `QNN_PLATFORM`     | `aarch64-oe-linux-gcc11.2`        | The lib subdir under `$QNN_SDK_ROOT/lib`. Override if you use a different toolchain (e.g. `aarch64-ubuntu-gcc9.4`). |
| `QNN_STUB_VERSION` | `v73`                             | Hexagon DSP version string (`v68` / `v69` / `v73` / `v75` / `v79`) – must match the SoC. |
| `CMAKE_BUILD_TYPE` | `Release`                         | Standard CMake build type. |
| `USE_MNN`          | `OFF`                             | Enable MNN backend. Not validated on Linux yet. |
| `USE_GGUF`         | `OFF`                             | Enable llama.cpp / GGUF backend. Not validated on Linux yet. |
| `BUILD_AS_DLL`     | `OFF`                             | Build `libGenieAPILibrary.so` instead of the executable. |

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

## 8. What was changed for Linux support

The Linux port is intentionally minimal. Reference list of edited files:

| File | Change |
|---|---|
| `samples/genie/c++/Service/CMakeLists.txt` | Allow Linux/Android in addition to Windows; add UNIX compile/link options. |
| `samples/genie/c++/Service/src/GenieAPIService/CMakeLists.txt` | Add UNIX (`add_executable`) branch; gate ARM64 MSVC platform fix to MSVC only; set `$ORIGIN` rpath. |
| `samples/genie/c++/Service/src/GenieAPIService/dependents.cmake` | Add Linux QNN platform / library naming, build `libappbuilder.so` and `libsamplerate` via `ExternalProject`. |
| `samples/genie/c++/Service/src/common/log.h` | ANSI color macros now also defined on Linux. |
| `samples/genie/c++/Service/src/common/utils.cpp` | `isPortAvailable` implemented with POSIX sockets. |
| `samples/genie/c++/Service/src/GenieAPIService/src/config.h` | Include `<unistd.h>` on non-Windows for `getcwd`. |
| `samples/genie/c++/build_linux.sh` (new) | One-click build script. |
| `samples/genie/c++/docs/BUILD_LINUX.md` (new) | This document. |

No business logic was changed.
