# Source code

## Service:

The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux
target.

When you finished building task , please check [USAGE](USAGE.MD) to learn how to use it.

## Android:

The code under this folder is Android app which can be used to launch the service in Android device.

## Build For Windows:

### Prepare environment:<br>

Use below command to clone the whole repository and the dependency 3rd party libraries.

```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

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

### Prepare the dependents

```
cd samples\genie\c++\External
git clone https://github.com/nothings/stb.git
```

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

Then the full release will locate at `Service\GenieSerivce_v2.1.3`

### Build GenieAPIServer for Android: <br>

Install Qualcomm® AI Runtime SDK, Android NDK etc, before you compile this service.<br>

```
cd ai-engine-direct-helper\samples\genie\c++\Service
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%
```

You should build these first

- [libappbuilder](https://github.com/quic/ai-engine-direct-helper/blob/main/BUILD.md)
- [libcurl](https://github.com/curl/curl)

Put `libcurl.so` and `libappbuilder.so` to  `ai-engine-direct-helper\samples\genie\c++\Service`

Then build GenieApiService for android

```
"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android -j4
```

When you finished building, please copy the following files.

```
copy "%QNN_SDK_ROOT%lib\aarch64-android\*.so"  "libs\arm64-v8a" /Y
copy "obj\local\arm64-v8a\*.so" "libs\arm64-v8a" /Y
```

### Build Android app:<br>

You can install Android Studio for building the Android app.

1. Open Android Studio then load android app project from `ai-engine-direct-helper\samples\genie\c++\Android`.


2. Click the Build menu then click Generate Signed App Bundle or Apk... then select APK and click Next button.
   then select your key and input your password, then click Next button. Finally, click Create buttton.


3. You can find the apk in ai-engine-direct-helper\samples\genie\c++\Android\app\release folder after finishing build.


4. Run adb install app-release.apk to install this apk.
