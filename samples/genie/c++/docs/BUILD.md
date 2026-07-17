# Source code

## Service:

The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux
target.

When you finished building task , please goto [USAGE](USAGE.MD) to learn how to use it.


### Prepare the repositories

Use below command to clone the whole repository and the dependency 3rd party libraries.

```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```

## Build For Windows:

### Prepare environment:<br>

Install these before you compile this service.<br>

- Qualcomm® AI Runtime SDK
- CMake
- Visual Studio Build Toos 2022(clang, v143)
- Ninja

Open a 'Command Prompt' window (not PowerShell) to compile the libraries.

### Set QAIRT SDK Version:<br>

After installing Qualcomm® AI Runtime SDK, it usually locates at `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`
(use whatever version you actually installed — this is only an example). We can make
it as an environment variable.

`Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\`

**`QNN_SDK_ROOT` must be set before configuring CMake**, since the build reads it at configure time.

### Build GenieAPIServer & GenieAPIClient:<br>

GenieServices can access the BIN/MNN/GGUF format AI model as capabilities.
**Windows/MSVC is the only platform that supports multiple backends at the same time** — you can add
`-DOption=ON` at the end of the CMake configure command to select it. Linux and Android builds are
fixed to QNN/Genie-only and do not accept these options (see [BUILD_LINUX.md](BUILD_LINUX.md) and
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

Then the full release will locate at `Service\GenieService_v<VERSION>` (currently `Service\GenieService_v2.3.7`,
see `scripts/version.cmake` for the authoritative version number).

## Build for Android: <br>

For Android builds, we provide an automated build script that handles all dependencies and generates a ready-to-install APK.

**Please refer to the [Android Build Guide](BUILD_ANDROID_README.md) for detailed instructions.**

The automated build script (`build_android.bat`) will:
- Build libappbuilder.so and all dependencies
- Build GenieAPIService
- Copy all required QNN SDK libraries
- Generate a signed Android APK with all libraries included

Simply run:
```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

For manual configuration and troubleshooting, see [BUILD_ANDROID_README.md](BUILD_ANDROID_README.md).

## Build For Linux (ARM64):

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

The output goes to `samples/genie/c++/Service/build-linux/GenieService_v<VERSION>_qnn<SDK_DIR_NAME>/`
(the Linux build is fixed to QNN/Genie-only; `USE_MNN`/`USE_GGUF` are not available here — see
[BUILD_LINUX.md](BUILD_LINUX.md) for details).

For all options, runtime setup, and troubleshooting see
[BUILD_LINUX.md](BUILD_LINUX.md).
