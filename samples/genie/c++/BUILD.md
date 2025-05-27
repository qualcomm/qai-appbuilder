# Source code
## Service:
  The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux target.

## Android:
  The code under this folder is Android app which can be used to launch the service in Android device.

## Build Service from source code:

### Build GenieAPIServer for WoS:<br>
Install Qualcomm® AI Runtime SDK, CMake, Visual Studio etc, before you compile this service.<br>
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64
cmake --build . --config Release
```

### Build GenieAPIServer for Android: <br>
Install Qualcomm® AI Runtime SDK, Android NDK etc, before you compile this service.<br>
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%

"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android
```

### Build Android app:<br>
You can install Android Studio for build the Android app.
