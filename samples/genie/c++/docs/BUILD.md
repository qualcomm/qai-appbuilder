# Source code

## Service:

The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux
target.

When you finished building task , please goto [USAGE](USAGE.MD) to learn how to use it.

### Prepare the repositories

Use below command to clone the whole repository and the dependency 3rd party libraries.

```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
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

After installing Qualcomm® AI Runtime SDK, It usually located at `C:\Qualcomm\AIStack\QAIRT\2.42.0.251225`. We can make
it as an environment variable.

`Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\`

### Build GenieAPIServer & GenieAPIClient:<br>

GenieServices can access the BIN/MNN/GGUF format AI model as capabilities.
you can add `-DOption=ON` and end of command to select it.

| Option     | Function                  | Default |
|------------|:--------------------------|---------|
| `USE_MNN`  | Support mnn format model  | OFF     |
| `USE_GGUF` | Support gguf format model | OFF     |

```
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 ..
cmake --build . --config Release --parallel 4
```

Then the full release will locate at `Service\GenieSerivce_v2.1.4`

### Build GenieAPIServer for Android: <br>

For Android builds, we provide an automated build script that handles all dependencies and generates a ready-to-install APK.

**Please refer to the [Android Build Guide](BUILD_ANDROID_README.md) for detailed instructions.**

The automated build script (`build_android.bat`) will:
- Build libappbuilder.so and all dependencies
- Build GenieAPIService
- Copy all required QNN SDK libraries
- Generate a signed Android APK with all libraries included

Simply run:
```cmd
cd ai-engine-direct-helper\samples\genie\c++
build_android.bat
```

For manual configuration and troubleshooting, see [BUILD_ANDROID_README.md](BUILD_ANDROID_README.md).
