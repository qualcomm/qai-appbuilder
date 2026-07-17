# Android Build Guide

## Overview

`build_android.bat` builds the Android version of `libappbuilder.so` and `GenieAPIService`, then packages them into a signed APK. All build artifacts land under `build-android/` (hyphen, not underscore); the source tree stays clean.

**Script location**: `qai-appbuilder/samples/genie/c++/Service/build_android.bat`

## Prerequisites

Export these as environment variables before running the script — it has no hardcoded default paths and fails fast with an explicit error if a required one is missing.

1. **Qualcomm® AI Runtime SDK (QAIRT)**
   `QNN_SDK_ROOT`, e.g. `C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\` (use whatever version you installed). The script requires `%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so` to exist.
2. **Android NDK**
   `NDK_ROOT` (or `ANDROID_NDK_ROOT`), recommended version r26d, e.g. `C:\work\android-ndk-r26d\`. Requires `%NDK_ROOT%\build\cmake\android.toolchain.cmake` and an `ndk-build.cmd`/`.bat` under `NDK_ROOT`.
3. **CMake** and **Ninja** — resolvable via `PATH` (used to build `libsamplerate`).
4. **Git** — for cloning the repository and its submodules.
5. **JDK** — `JAVA_HOME` must be set, or `java` resolvable via `PATH` (used by Gradle). JDK 17 is the version validated for this project.

## Configuration

Set these before invoking the script (Command Prompt syntax; from PowerShell use `$env:NAME = "value"`):

```batch
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
set "JOBS=4"
```

| Variable | Required | Meaning |
|----------|----------|---------|
| `QNN_SDK_ROOT` | Yes | Path to your QAIRT SDK installation. |
| `NDK_ROOT` / `ANDROID_NDK_ROOT` | Yes | Path to your Android NDK installation. |
| `JOBS` | No | Parallel compilation jobs. Defaults to `4`. |
| `GRADLE_HOME` | No | Path to an already-extracted Gradle installation. When set, the script calls its `bin\gradle.bat` directly, bypassing the wrapper and `GRADLE_DISTRIBUTION_ZIP` entirely. |
| `GRADLE_DISTRIBUTION_ZIP` | No | Path to a local Gradle distribution zip. Ignored if `GRADLE_HOME` is set. Otherwise the script rewrites `distributionUrl` to use it instead of downloading from services.gradle.org. |

### Gradle configuration

Gradle, the Android Gradle Plugin, and the NDK for the app module's own native build (`app/src/main/cpp`, unrelated to the QAIRT libraries) all resolve automatically — no manual configuration needed. Already have Gradle installed? Set `GRADLE_HOME` to it and the script calls it directly, skipping the wrapper entirely. Otherwise, offline or behind a firewall, set `GRADLE_DISTRIBUTION_ZIP` (see "Configuration" above) instead of editing `gradle-wrapper.properties` by hand. With neither set, the wrapper still downloads its distribution automatically, but into `build-android\gradle-home` instead of the default `~/.gradle`, so the build stays self-contained. If Gradle can't resolve an NDK, set `ndk.dir` in `Android/local.properties` or add `ndkVersion` under `android {}`.

### Release signing

Android refuses to install an unsigned APK, sideloading included — this is an OS-level rule, not a choice made by this project. `build_android.bat` always runs `assembleRelease`, which requires a signing config to succeed.

`Android/app/build.gradle.kts` ships with a placeholder `release` signing config pointing at `C:\work\Android\genieapiservice` (password `123456`). Generate your own keystore instead of reusing it, and update `storeFile`/`storePassword`/`keyAlias`/`keyPassword` accordingly:

```cmd
keytool -genkey -v -keystore C:\work\Android\genieapiservice.jks -alias key0 -keyalg RSA -keysize 2048 -validity 10000 -storepass 123456 -keypass 123456 -dname "CN=GenieAPIService, OU=Dev, O=Dev, L=Unknown, ST=Unknown, C=US"
```

The `-storepass`/`-keypass`/`-dname` flags make this run unattended, with no interactive prompts; replace the passwords and DN with your own values.

No release build needed? Run `gradlew.bat assembleDebug` from `Android/` instead — AGP signs debug builds automatically with a disposable debug keystore, no configuration required. `build_android.bat` only runs `assembleRelease`; for a debug APK, run steps 1-5 of "Build steps" below to stage the native libraries, then invoke Gradle directly.

## Usage

```cmd
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
cd qai-appbuilder\samples\genie\c++\Service
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"
set "NDK_ROOT=C:\work\android-ndk-r26d\"
build_android.bat
```

Run it from Command Prompt (CMD). The script itself is a plain `.bat`; if you set the environment variables from PowerShell first, PowerShell can still invoke it directly.

### Build steps

The script runs these steps in order and stops on the first failure:

1. Validate the environment (required env vars, `ninja`/`cmake` on `PATH`, the QNN Android runtime lib, the NDK's `android.toolchain.cmake`, `gradlew.bat`, a usable JDK).
2. Build `libsamplerate.so` via CMake + Ninja against the Android NDK toolchain, with `-DBUILD_SHARED_LIBS=ON` (the vendored source defaults to a static build).
3. Build `libappbuilder.so` via `ndk-build`, using the repository's top-level `make/Android.mk` and `make/Application.mk` — the same official path documented in the repository's top-level `Build.md`/`Makefile`. This is *not* a CMake build of `src/`.
4. Build the native `GenieAPIService`/JNI wrapper via `ndk-build`, using `scripts/Android.mk` and `scripts/Application.mk`.
5. Copy the QNN SDK runtime libraries (`libGenie.so`, `libQnnHtp*.so`, `libQnnSystem.so`, the Hexagon stub/skel/`.cat` files for `QNN_STUB_VERSION`) plus the freshly built `.so` files into `build-android\output\libs\arm64-v8a`.
6. Run `gradlew.bat assembleRelease -PqnnStubVersion=<QNN_STUB_VERSION>` to produce the signed APK.
7. Copy the generated APK to `build-android\output\GenieAPIService.apk`.

## Build output

```
build-android/
├── libsamplerate/            # libsamplerate CMake+Ninja build tree
├── libsamplerate.so          # copied out of libsamplerate/ for convenience
├── libappbuilder-obj/        # ndk-build intermediates for libappbuilder (make/Android.mk)
├── libappbuilder-libs/       # ndk-build output for libappbuilder
├── libappbuilder.so          # copied out of libappbuilder-libs/arm64-v8a/ for convenience
├── obj/                      # ndk-build intermediates for the native service
├── libs/
│   └── arm64-v8a/            # ndk-build output for the native service + JNI wrapper
└── output/
    ├── libs/
    │   └── arm64-v8a/         # all deployable .so files, consolidated here
    └── GenieAPIService.apk    # Android APK package
```

- **`build-android/output/libs/arm64-v8a/`**: every `.so` file needed for Android deployment.
- **`build-android/output/GenieAPIService.apk`**: the built Android APK package.

## Install APK

```cmd
adb install build-android/output/GenieAPIService.apk
```

## Troubleshooting

**`[ERROR] QNN_SDK_ROOT is not set.`**
Export `QNN_SDK_ROOT` before running the script, e.g. `set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.44.0.260225\"`.

**`[ERROR] NDK_ROOT or ANDROID_NDK_ROOT is not set.`**
Export `NDK_ROOT` (or `ANDROID_NDK_ROOT`) before running the script.

**Build fails with "cannot find header files"**
Clone with `--recursive`, or run `git submodule update --init --recursive` to fetch missing submodules.

**Gradle build fails**
If Gradle can't download its distribution, set `GRADLE_HOME` to an existing Gradle install, or `GRADLE_DISTRIBUTION_ZIP` to a local zip (see "Gradle configuration" above) instead of editing `gradle-wrapper.properties` directly. If you changed the Android Gradle Plugin version in `libs.versions.toml`, verify it's still valid. Confirm JDK is installed and configured.

**`Could not resolve all files for configuration ':app:androidJdkImage'`**

```
Execution failed for task ':app:compileReleaseJavaWithJavac'.
> Could not resolve all files for configuration ':app:androidJdkImage'.
   > Failed to transform core-for-system-modules.jar...
```

This happens when Android Studio's embedded JDK is incompatible with the Gradle version. Fix it one of these ways:

- **Use JDK 17 (recommended)**: in Android Studio, go to **File → Settings → Build, Execution, Deployment → Build Tools → Gradle**, set "Gradle JDK" to **JDK 17**, apply, then clean and rebuild.
- **Set `JAVA_HOME` manually**:
  ```cmd
  set JAVA_HOME=C:\Program Files\Java\jdk-17
  cd qai-appbuilder\samples\genie\c++\Android
  gradlew.bat clean assembleRelease
  ```

**Clean build artifacts**
Delete the `build-android` directory: `rmdir /s /q build-android`.

**Change the number of parallel jobs**
Set `JOBS` before running the script, e.g. `set "JOBS=8"` (defaults to `4`). Use your CPU core count or slightly less.

## Version information

- Supported NDK version: r26d
- Supported target architecture: arm64-v8a
- Supported QAIRT version: 2.44.0.260225 (use whatever version you installed — this is only the version validated for this project)
- Gradle: 8.5 · Android Gradle Plugin: 8.1.4 (see "Gradle configuration" above)

## License

This script follows the same license as the qai-appbuilder project (BSD-3-Clause).
