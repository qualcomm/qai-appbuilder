# Android Build Guide

## Overview

`build_android.bat` is an automated build script for compiling the Android version of libappbuilder.so and GenieAPIService libraries. The script automatically handles dependencies and organizes all build artifacts into the `build-android` folder (note the hyphen, not an underscore), keeping the source code directory clean.

**Script Location**: `qai-appbuilder/samples/genie/c++/Service/build_android.bat`

## Features

✅ Automatically builds libappbuilder.so and its dependencies
✅ Automatically builds GenieAPIService native library
✅ Automatically copies all required QNN SDK library files
✅ Organizes all build artifacts into a separate `build-android` directory
✅ Provides build logs and stops immediately on the first failed step
✅ Environment validation and error checking (required env vars, tools on `PATH`, expected SDK/NDK files)
✅ Builds Android APK package with all libraries included

## Prerequisites

Before running the script, ensure the following tools are installed and, unlike previous versions of this script, exported as **environment variables** before invoking it (the script no longer has any hardcoded default paths — it fails fast with an explicit error message if a required variable is missing):

1. **Qualcomm® AI Runtime SDK (QAIRT)**
   - `QNN_SDK_ROOT`, e.g. `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\` (use whatever version you actually installed — this is only an example; the script requires `%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so` to exist)

2. **Android NDK**
   - `NDK_ROOT` (or `ANDROID_NDK_ROOT`), recommended version r26d, e.g. `C:\work\android-ndk-r26d\`
   - Requires `%NDK_ROOT%\build\cmake\android.toolchain.cmake` and an `ndk-build.cmd`/`.bat` to exist under `NDK_ROOT`

3. **CMake** and **Ninja** — both must be resolvable via `PATH` (used to build `libsamplerate`/`libappbuilder`)

4. **Git** (for cloning the repository)

5. **Java Development Kit (JDK)** — `JAVA_HOME` must be set, or `java` must be resolvable via `PATH` (used by the Gradle build). JDK 17 is the version validated for this project.

## Configuration

### Set environment variables before running `build_android.bat`

Unlike earlier versions of this script, you no longer need to edit `build_android.bat` itself to point it at your SDK/NDK — the script reads everything from environment variables and fails fast with an explicit `[ERROR] ... is not set` message if a required one is missing.

Set these before invoking the script (Command Prompt syntax shown; if you run it from PowerShell use `$env:NAME = "value"` instead):

```batch
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
set "JOBS=4"
```

**Configuration Parameters:**

- **QNN_SDK_ROOT**: Path to your Qualcomm AI Runtime SDK installation. *(required; use whatever version you actually installed, `2.44.0.260225` above is only an example)*
- **NDK_ROOT** (or **ANDROID_NDK_ROOT**): Path to your Android NDK installation. *(required)*
- **JOBS**: Number of parallel compilation jobs. *(optional, defaults to `4` if unset)*

### Gradle project files (reference only — verify against your actual project)

> **Note:** This C++ service source tree does not ship the `Android/` Gradle project directory by
> itself, so the exact values below cannot be verified against this repository snapshot. Treat
> them as a **reference example** of a typical Gradle setup and always check your own
> `Android/app/build.gradle.kts`, `Android/gradle/wrapper/gradle-wrapper.properties`, and
> `Android/gradle/libs.versions.toml` for the values that actually apply to your project.

##### `samples/genie/c++/Android/gradle/wrapper/gradle-wrapper.properties`

Points Gradle at a local distribution zip, e.g.:

```properties
distributionUrl=file\:///C:/Programs/gradle-8.7-bin.zip
```

Change it to match wherever your Gradle distribution actually lives.

##### `samples/genie/c++/Android/gradle/libs.versions.toml`

Android Gradle Plugin version, e.g.:

```toml
[versions]
agp = "8.7.3"  # Adjust to match your installed version
```

##### `samples/genie/c++/Android/app/build.gradle.kts`

**IMPORTANT**: Configure APK signing for release builds:

```kotlin
android {
    compileSdk = 35  // Adjust if needed
    ndkVersion = "26.3.11579264"  // Match your NDK version
    
    // Configure signing for release builds
    signingConfigs {
        create("release") {
            storeFile = file("C:\\work\\Android\\genieapiservice")  // ⚠️ CHANGE THIS
            storePassword = "123456"                                 // ⚠️ CHANGE THIS
            keyAlias = "key0"                                        // ⚠️ CHANGE THIS
            keyPassword = "123456"                                   // ⚠️ CHANGE THIS
        }
    }
    
    defaultConfig {
        minSdk = 29
        targetSdk = 35  // Adjust if needed
    }
    
    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
        }
    }
}
```

**Signing Configuration Parameters:**

- **storeFile**: Path to your keystore file (`.jks` or `.keystore`)
- **storePassword**: Password for the keystore
- **keyAlias**: Alias name of the key in the keystore
- **keyPassword**: Password for the key

**How to create a keystore (if you don't have one):**

```cmd
keytool -genkey -v -keystore C:\work\Android\genieapiservice.jks -alias key0 -keyalg RSA -keysize 2048 -validity 10000
```

Follow the prompts to set passwords and certificate information.

## Usage

### 1. Clone the Repository (if not already done)

```cmd
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder\samples\genie\c++\Service
```

### 2. Set the Required Environment Variables

**IMPORTANT**: Follow the "Configuration" section above to export `QNN_SDK_ROOT`/`NDK_ROOT` (and, if needed, review the Gradle project files) — there is no need to edit `build_android.bat` itself.

### 3. Run the Build Script

Execute in Command Prompt (CMD):

```cmd
build_android.bat
```

**Note**: Run it from Command Prompt (CMD). If you use PowerShell, set the environment variables with
`$env:NAME = "value"` first — the script itself is a plain `.bat` and PowerShell can invoke it directly.

### 4. Wait for Build Completion

The script executes the following steps in order (there is no numbered `[n/9]`-style progress output;
this is simply the real sequence found in `build_android.bat`):

1. Validate the environment: required env vars (`QNN_SDK_ROOT`, `NDK_ROOT`/`ANDROID_NDK_ROOT`), `ninja`/`cmake` on `PATH`, the QNN Android runtime lib, the NDK's `android.toolchain.cmake`, `gradlew.bat`, and a usable JDK.
2. Create the `build-android` directory and `build-android\output\libs\arm64-v8a`.
3. Build `libsamplerate.so` via **CMake + Ninja** against the Android NDK toolchain, with `-DBUILD_SHARED_LIBS=ON` (the vendored `External/libsamplerate` defaults to a static build, so this flag has to be passed explicitly).
4. Build `libappbuilder.so` via **CMake + Ninja** as well, from the repository's top-level `src/` directory (the same source that `qai_appbuilder` Python wheels link against) — **not** via `ndk-build`.
5. Build the native `GenieAPIService`/JNI wrapper via `ndk-build`, using `scripts/Android.mk` + `scripts/Application.mk` (this is the only step in the pipeline that actually uses `ndk-build`).
6. Copy the QNN SDK runtime libraries (`libGenie.so`, `libQnnHtp*.so`, `libQnnSystem.so`, the Hexagon stub/skel/`.cat` files for `QNN_STUB_VERSION`) plus the freshly built `.so` files into `build-android\output\libs\arm64-v8a`.
7. Run `gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>` to produce the signed APK.
8. Copy the generated APK to `build-android\output\GenieAPIService.apk`.
9. Print a short build summary (native lib output dir + APK path).

## Build Output

After a successful build, all files are organized in the `build-android` directory (hyphen, not underscore):

```
build-android/
├── libsamplerate/            # libsamplerate CMake+Ninja build tree
├── libappbuilder/            # libappbuilder.so CMake+Ninja build tree
├── libsamplerate.so          # copied out of libsamplerate/ for convenience
├── libappbuilder.so          # copied out of the top-level src/lib/ for convenience
├── obj/                      # ndk-build intermediates for the native service
├── libs/
│   └── arm64-v8a/            # ndk-build output for the native service + JNI wrapper
└── output/
    ├── libs/
    │   └── arm64-v8a/         # All deployable .so files, consolidated here
    │       ├── libappbuilder.so
    │       ├── libsamplerate.so
    │       ├── libGenieAPIService.so
    │       ├── libJNIGenieAPIService.so
    │       ├── libGenie.so
    │       ├── libQnnHtp*.so
    │       └── ... (other QNN SDK runtime libraries)
    └── GenieAPIService.apk    # Android APK package (directly under output/, no apk/ subfolder)
```

### Key Output Directories

- **`build-android/output/libs/arm64-v8a/`**: Contains all .so files needed for Android deployment
- **`build-android/output/GenieAPIService.apk`**: The built Android APK package

## Install APK

After building, install the APK directly:

```cmd
adb install build-android/output/GenieAPIService.apk
```

## Troubleshooting

### Q1: Script error "[ERROR] QNN_SDK_ROOT is not set."

**Solution**: 
- Verify QAIRT SDK is correctly installed
- Export `QNN_SDK_ROOT` as an environment variable before running the script (e.g. `set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"`) — you no longer edit the script itself

### Q2: Script error "[ERROR] NDK_ROOT or ANDROID_NDK_ROOT is not set."

**Solution**:
- Verify Android NDK is correctly installed
- Export `NDK_ROOT` (or `ANDROID_NDK_ROOT`) as an environment variable before running the script

### Q3: Build fails with "cannot find header files"

**Solution**:
- Ensure you cloned the repository with `--recursive` flag to include all submodules
- Run `git submodule update --init --recursive` to update submodules

### Q4: Gradle build fails

**Solution**:
- Verify the Gradle distribution path in `gradle-wrapper.properties` and the Android Gradle Plugin version in `libs.versions.toml` against your actual `Android/` project (these values could not be verified from this repository snapshot — see the note in the "Configuration" section)
- Ensure JDK is properly installed and configured

### Q4.1: Android Studio build fails with "Could not resolve all files for configuration ':app:androidJdkImage'"

**Error Message:**
```
Execution failed for task ':app:compileReleaseJavaWithJavac'.
> Could not resolve all files for configuration ':app:androidJdkImage'.
   > Failed to transform core-for-system-modules.jar...
```

**Solution**:

This error occurs when Android Studio's embedded JDK is incompatible with the Gradle version. Try these solutions:

**Option 1: Use JDK 17 (Recommended)**

1. In Android Studio, go to **File → Settings → Build, Execution, Deployment → Build Tools → Gradle**
2. Under "Gradle JDK", select **JDK 17** (or download it if not available)
3. Click **Apply** and **OK**
4. Clean and rebuild the project

**Option 2: Use the build_android.bat script**

The automated build script only checks that `JAVA_HOME`/`java` is available; it does **not**
automatically resolve JDK/Gradle version incompatibilities. If Option 1 or 3 doesn't apply to your
setup, make sure whatever JDK the script picks up is Gradle-compatible before running it:
```cmd
cd qai-appbuilder\samples\genie\c++\Service
build_android.bat
```

**Option 3: Set JAVA_HOME manually**

If you have JDK 17 installed separately:
```cmd
set JAVA_HOME=C:\Program Files\Java\jdk-17
cd qai-appbuilder\samples\genie\c++\Android
gradlew.bat clean assembleRelease
```

### Q5: How to clean build artifacts?

**Solution**:
- Delete the `build-android` directory: `rmdir /s /q build-android`
- The source code directory remains clean with no build artifacts

### Q6: Can I change the number of parallel jobs?

**Solution**:
- Yes, set the `JOBS` environment variable to your desired value before running the script, e.g. `set "JOBS=8"` (defaults to `4` if unset)
- Recommended: Set to your CPU core count or slightly less

## Script Features

### 1. Environment Validation
The script validates all required environment variables, tools on `PATH`, and expected SDK/NDK files before building, and prints an explicit `[ERROR] ...` message identifying exactly what's missing.

### 2. Error Handling
Each build step checks its own exit code (`if errorlevel 1 exit /b 1`); the script stops immediately if any step fails.

### 3. Build Artifact Isolation
All build artifacts are stored in the `build-android` directory, keeping the source code directory clean.

### 4. Automatic Dependency Handling
The script builds `libsamplerate` and `libappbuilder.so` before the native `GenieAPIService`/JNI wrapper that depends on them, in the correct order.

### 5. Build Progress Output
Prints the resolved configuration (SDK/NDK paths, stub version, job count, output dir) and per-step progress to the console.

### 6. APK Generation
Automatically builds an Android APK package ready for installation.

## Technical Details

### Build Process

1. **libsamplerate.so Build**
   - Uses **CMake + Ninja** against the Android NDK toolchain file
   - Target architecture: arm64-v8a
   - Built with `-DBUILD_SHARED_LIBS=ON` (the vendored source defaults to static)

2. **libappbuilder.so Build**
   - Also uses **CMake + Ninja** (not `ndk-build`) against the Android NDK toolchain file
   - Target architecture: arm64-v8a
   - Source: the repository's top-level `src/` directory (same source `qai_appbuilder` Python wheels link against)

3. **GenieAPIService Build**
   - Depends on libappbuilder.so
   - This is the only step that actually uses Android NDK's `ndk-build` tool
   - Build configuration: `scripts/Android.mk` and `scripts/Application.mk`

4. **Android APK Build**
   - Uses Gradle build system (`gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>`)
   - Packages all native libraries
   - Configuration: `Android/app/build.gradle.kts` *(reference only — see the note in "Configuration")*

5. **Library Collection**
   - Copies runtime libraries from QNN SDK
   - Copies generated libraries from build output
   - Consolidates all files to `build-android/output/libs/arm64-v8a/`


## Version Information

- **Script Version**: 2.0
- **Supported NDK Version**: r26d
- **Supported Target Architecture**: arm64-v8a
- **Supported QAIRT Version**: 2.44.0.260225 (use whatever version you actually installed — this is only the version validated for this project)
- **Gradle Version / Android Gradle Plugin**: see the disclaimer in "Configuration" — cannot be verified from this repository snapshot; check your own `Android/gradle/` project files

## License

This script follows the same license as the qai-appbuilder project (BSD-3-Clause).

## Feedback and Support

If you encounter issues:

1. Check the "Troubleshooting" section in this document
2. Verify your environment configuration is correct
3. Review error messages in the build log

---

**Last Updated**: 2026-04-17
