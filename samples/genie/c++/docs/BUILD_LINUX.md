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
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.45.40.260406/v2.45.40.260406.zip
unzip v2.45.40.260406.zip
export QNN_SDK_ROOT=$(pwd)/qairt/2.45.40.260406
export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2
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

### Output

The final artefacts will be placed in
`samples/genie/c++/Service/GenieService_v<APPVER>_qnn<SDKVER>/`.

For example, with app version 2.1.5 and QAIRT SDK 2.45.40:
```
Service/GenieService_v2.1.5_qnn2.45.40/
├── GenieAPIService               # the executable
└── libappbuilder.so              # built from the top-level src/
```

### Build with GGUF enabled

To enable GGUF support:
```bash
cd samples/genie/c++
chmod +x build_linux.sh
USE_GGUF=ON ./build_linux.sh
```

If `USE_GGUF=ON`, the directory name gets `_gguf` appended and additionally
contains the llama.cpp shared libraries:

```
Service/GenieService_v2.1.5_qnn2.45.40_gguf/
├── GenieAPIService               # the executable
├── libappbuilder.so              # built from the top-level src/
├── libllama.so.0                 # llama.cpp main library
├── libggml.so.0                  # ggml backend entry
├── libggml-cpu.so.0              # ggml CPU backend
└── libggml-base.so.0             # ggml base runtime
```

> **Note:** The Linux output is intentionally minimal — just the binaries
> you need to deploy to a target board that already has the QAIRT SDK
> installed. The QNN runtime libs (`libGenie.so`, `libQnnHtp.so`, etc.) are
> NOT copied because they already exist on the target device as part of the
> SDK installation.
>
> On Windows the output directory keeps the shorter name
> `GenieService_v<APPVER>` and includes the full SDK runtime for standalone
> deployment.

### Build options

`build_linux.sh` reads the **same environment variables that QAI AppBuilder
uses** (so the values you already export for `python -m build -w` work here
without any change):

| Variable           | Default                           | Meaning |
|--------------------|-----------------------------------|---------|
| `QNN_SDK_ROOT`     | *(required)*                      | Path to the extracted QAIRT SDK. |
| `QAI_TOOLCHAINS`   | `aarch64-oe-linux-gcc11.2`        | Toolchain subdirectory under `$QNN_SDK_ROOT/lib/`. |
| `BUILD_TYPE`       | `Release`                         | Standard CMake build type. |
| `JOBS`             | `$(nproc)`                        | Parallel jobs for the build. |
| `USE_MNN`          | `OFF`                             | Enable MNN backend. Not validated on Linux yet. |
| `USE_GGUF`         | `OFF`                             | Enable llama.cpp / GGUF backend (see "Build with GGUF enabled" above). |
| `BUILD_AS_DLL`     | `OFF`                             | Build `libGenieAPILibrary.so` instead of the executable. |
| `BUILD_LINUX_CLIENT` | `OFF`                           | Build the `GenieAPIClient` sample. libcurl is built from `External/curl/` via `ExternalProject`. If HTTPS support is needed: `sudo apt install libssl-dev`. |

---

## 5. Run

Set the runtime library paths and start the service:

```bash
export QNN_SDK_ROOT=/absolute/path/to/2.45.40.260406

# OUT_DIR is the directory printed by build_linux.sh as "Output dir: ...".
# Its name follows the pattern GenieService_v<APPVER>_qnn<SDKVER>[_gguf].
# Example for app 2.1.5 + QAIRT 2.45.40 (no GGUF):
OUT_DIR=$(pwd)/samples/genie/c++/Service/GenieService_v2.1.5_qnn2.45.40

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
(`GenieService_v<APPVER>_qnn<SDKVER>[_gguf]/`) to the target device. The
build only produces runtime-essential binaries; every file in that directory
is needed.

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
by the model config is used for indicate HTP version. Please set `dsp_arch` to correct value. For example:

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

### 5.2. Running with the GGUF backend

When built with `USE_GGUF=ON`, the output directory contains additional
`lib*.so.0` shared libraries from llama.cpp (see section 4 "Build with GGUF
enabled" for the full file list).

The `GenieAPIService` executable was built with `INSTALL_RPATH=$ORIGIN`,
so it automatically locates these `lib*.so.0` files **as long as they sit
in the same directory as the binary**. You do **not** need to add the
output directory to `LD_LIBRARY_PATH` for the GGUF libraries; only the
QAIRT SDK lib dir still has to be on `LD_LIBRARY_PATH`, exactly as in
the non-GGUF case above.

If you copy `GenieAPIService` somewhere else, copy all the `lib*.so.0`
files alongside it (or add the original output directory to
`LD_LIBRARY_PATH`).

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
`build_linux.sh` also performs an early submodule presence check and
will print the same advice before invoking CMake.

**`Could NOT find OpenSSL` (only when `BUILD_LINUX_CLIENT=ON`)**
The `External/curl/` `ExternalProject` needs OpenSSL headers to build
HTTPS support. Install them with:
```bash
sudo apt install libssl-dev
```
