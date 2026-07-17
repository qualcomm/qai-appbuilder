## Service

This folder contains the C++ implementation of the service. It compiles for Windows, Android, and Linux.

Once built, see [USAGE](USAGE.MD) for how to use it.

### Get the source

Clone the whole repository, including the third-party dependencies:

```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```

## Build for Windows

### Prepare the environment

Install these before compiling:

- Qualcomm® AI Runtime SDK
- CMake
- Visual Studio Build Tools 2022 (clang, v143)
- Ninja

Use a Command Prompt window, not PowerShell, to compile.

### Set the QAIRT SDK version

After installing Qualcomm® AI Runtime SDK, it's usually located at `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`
(use whatever version you actually installed — this is only an example). Export it as an
environment variable:

`Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`

**`QNN_SDK_ROOT` must be set before configuring CMake**, since the build reads it at configure time.

### Build GenieAPIService & GenieAPIClient

GenieServices can access BIN/MNN/GGUF format AI models as capabilities.
**Windows/MSVC is the only platform that supports multiple backends at the same time** — add
`-DOption=ON` at the end of the CMake configure command to select one. Linux and Android builds are
fixed to QNN/Genie-only and don't accept these options (see [BUILD_LINUX.md](BUILD_LINUX.md) and
[BUILD_ANDROID_README.md](BUILD_ANDROID_README.md)).

| Option         | Function                                                            | Default |
|----------------|:---------------------------------------------------------------------|---------|
| `USE_MNN`      | Support mnn format model (CPU backend)                             | OFF     |
| `USE_GGUF`     | Support gguf format model (GPU backend via llama.cpp, falls back to CPU) | OFF     |
| `BUILD_AS_DLL` | Build only the `GenieAPILibrary.dll` target. Default OFF builds the full set (exe + dll + all examples: `GenieAPIClient`, `SampleApp`, `examples/tools`). | OFF |

```
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 -DUSE_MNN=ON -DUSE_GGUF=ON
cmake --build . --config Release --parallel 4
```

Then the full release is located at `Service\GenieService-win-arm64\` (fixed name, no version suffix;
set by `BUILD_PATH` in `Service/CMakeLists.txt`).

## Build for Android

An automated script handles all dependencies and produces a ready-to-install APK.

**See the [Android Build Guide](BUILD_ANDROID_README.md) for full instructions.**

`build_android.bat`:
- Builds `libappbuilder.so` and its dependencies
- Builds `GenieAPIService`
- Copies the required QNN SDK libraries
- Generates a signed Android APK with all libraries included

```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

For manual configuration and troubleshooting, see [BUILD_ANDROID_README.md](BUILD_ANDROID_README.md).

## Build for Linux (ARM64)

Building the service on a Qualcomm-based ARM64 Linux device uses the **same
toolchain and SDK** as the QAI AppBuilder Linux build. If your machine can
already build the `qai_appbuilder` Python wheel, it can build GenieAPIService
without any extra setup.

**One-line build:**

```bash
cd samples/genie/c++
export QNN_SDK_ROOT=/absolute/path/to/QAIRT_SDK
chmod +x build_linux.sh
./build_linux.sh
```

The output goes to `samples/genie/c++/Service/build-linux/GenieService-linux-arm64/`
(fixed name, no version suffix; the Linux build is fixed to QNN/Genie-only — `USE_MNN`/`USE_GGUF`
are not available here; see [BUILD_LINUX.md](BUILD_LINUX.md) for details).

For all options, runtime setup, and troubleshooting see
[BUILD_LINUX.md](BUILD_LINUX.md).
