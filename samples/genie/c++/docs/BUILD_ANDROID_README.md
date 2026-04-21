# Android Build Guide

## Overview

`build_android.bat` is an automated build script for compiling the Android version of libappbuilder.so and GenieAPIService libraries. The script automatically handles dependencies and organizes all build artifacts into the `build_android` folder, keeping the source code directory clean.

**Script Location**: `ai-engine-direct-helper/samples/genie/c++/build_android.bat`

## Features

✅ Automatically builds libappbuilder.so and its dependencies  
✅ Automatically builds GenieAPIService library  
✅ Automatically copies all required QNN SDK library files  
✅ Organizes all build artifacts into a separate build directory  
✅ Provides detailed build logs and error messages  
✅ Environment validation and error checking  
✅ Builds Android APK package with all libraries included  
✅ **Android file logging support** - All QNN log functions write to files  
✅ Automatic Java 17 download if Java 25 is detected

## Prerequisites

Before running the script, ensure the following tools are installed:

1. **Qualcomm® AI Runtime SDK (QAIRT)**
   - Default path: `C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\`

2. **Android NDK**
   - Recommended version: r26d
   - Default path: `C:\work\android-ndk-r26d-windows\android-ndk-r26d\`

3. **Git** (for cloning the repository)

4. **Java Development Kit (JDK)** (for building APK)

## Manual Configuration Required

### IMPORTANT: Before Running build_android.bat

You **MUST** manually modify the following paths in the script according to your environment:

#### 1. Edit `build_android.bat`

Open `build_android.bat` and modify these configuration variables:

```batch
REM ============================================================================
REM Configuration - MODIFY THESE PATHS ACCORDING TO YOUR ENVIRONMENT
REM ============================================================================

REM Set QNN SDK Root (Qualcomm AI Runtime SDK)
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\"

REM Set Android NDK Root
set "NDK_ROOT=C:\work\android-ndk-r26d-windows\android-ndk-r26d\"

REM Number of parallel jobs for GenieAPIService build
set "JOBS=4"
```

**Configuration Parameters:**

- **QNN_SDK_ROOT**: Path to your Qualcomm AI Runtime SDK installation
- **NDK_ROOT**: Path to your Android NDK installation
- **JOBS**: Number of parallel compilation jobs (adjust based on your CPU cores)

#### 2. Edit Gradle Configuration Files

##### 2.1 Update `samples/genie/c++/Android/gradle/wrapper/gradle-wrapper.properties`

Modify the Gradle distribution URL to match your environment:

```properties
distributionUrl=file\:///C:/Programs/gradle-8.7-bin.zip
```

Change `C:/Programs/gradle-8.7-bin.zip` to your actual Gradle distribution path.

##### 2.2 Update `samples/genie/c++/Android/gradle/libs.versions.toml`

Modify the Android Gradle Plugin version if needed:

```toml
[versions]
agp = "8.7.3"  # Adjust to match your installed version
```

##### 2.3 Update `samples/genie/c++/Android/app/build.gradle.kts`

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
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
cd ai-engine-direct-helper
```

### 2. Configure the Build Script

**IMPORTANT**: Follow the "Manual Configuration Required" section above to modify all necessary paths.

### 3. Run the Build Script

Execute in Command Prompt (CMD):

```cmd
build_android.bat
```

**Note**: Use Command Prompt (CMD), not PowerShell.

### 4. Wait for Build Completion

The script will automatically execute the following steps:

1. **[1/9]** Validate environment configuration
2. **[2/9]** Create build directory structure
3. **[3/9]** Build libsamplerate.so
4. **[4/9]** Skip libcurl.so (not required)
5. **[5/9]** Build libappbuilder.so
6. **[6/9]** Copy dependencies to Service directory
7. **[7/9]** Build GenieAPIService
8. **[8/9]** Build Android APK
9. **[9/9]** Copy all library files to output directory
10. **[10/10]** Display build summary

## Build Output

After successful build, all files will be organized in the `build_android` directory:

```
build_android/
├── appbuilder/              # libappbuilder.so build artifacts
│   ├── libappbuilder.so
│   ├── libs/
│   └── obj/
├── service/                 # GenieAPIService build artifacts
│   ├── libs/
│   └── obj/
└── output/
    ├── libs/
    │   └── arm64-v8a/       # All deployable .so files
    │       ├── libappbuilder.so
    │       ├── libGenieAPIService.so
    │       ├── libJNIGenieAPIService.so
    │       ├── libGenie.so
    │       ├── libQnnHtp.so
    │       └── ... (other QNN SDK libraries)
    └── apk/
        └── GenieAPIService.apk  # Android APK package
```

### Key Output Directories

- **`build_android/output/libs/arm64-v8a/`**: Contains all .so files needed for Android deployment
- **`build_android/output/apk/`**: Contains the built Android APK package

## Install APK

After building, install the APK directly:

```cmd
adb install build_android/output/apk/GenieAPIService.apk
```

## Troubleshooting

### Q1: Script error "QNN_SDK_ROOT not found"

**Solution**: 
- Verify QAIRT SDK is correctly installed
- Modify `QNN_SDK_ROOT` path in the script to match your actual installation path

### Q2: Script error "NDK_ROOT not found"

**Solution**:
- Verify Android NDK is correctly installed
- Modify `NDK_ROOT` path in the script to match your actual installation path

### Q3: Build fails with "cannot find header files"

**Solution**:
- Ensure you cloned the repository with `--recursive` flag to include all submodules
- Run `git submodule update --init --recursive` to update submodules

### Q4: Gradle build fails

**Solution**:
- Verify Gradle distribution path in `gradle-wrapper.properties`
- Check Android Gradle Plugin version in `libs.versions.toml`
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

The automated build script handles JDK compatibility automatically:
```cmd
cd ai-engine-direct-helper\samples\genie\c++
build_android.bat
```

**Option 3: Set JAVA_HOME manually**

If you have JDK 17 installed separately:
```cmd
set JAVA_HOME=C:\Program Files\Java\jdk-17
cd ai-engine-direct-helper\samples\genie\c++\Android
gradlew.bat clean assembleRelease
```

### Q5: How to clean build artifacts?

**Solution**:
- Delete the `build_android` directory: `rmdir /s /q build_android`
- The source code directory remains clean with no build artifacts

### Q6: Can I change the number of parallel jobs?

**Solution**:
- Yes, modify `set "JOBS=4"` in the script to your desired value
- Recommended: Set to your CPU core count or slightly less

## Script Features

### 1. Environment Validation
The script validates all required tools and paths before building.

### 2. Error Handling
Each build step includes error checking; the script stops immediately if any step fails.

### 3. Build Artifact Isolation
All build artifacts are stored in the `build_android` directory, keeping the source code directory clean.

### 4. Automatic Dependency Handling
The script automatically handles dependencies between libappbuilder.so and GenieAPIService.

### 5. Detailed Logging
Provides detailed build progress and status information for easy debugging.

### 6. APK Generation
Automatically builds an Android APK package ready for installation.

## Technical Details

### Build Process

1. **libsamplerate.so Build**
   - Uses CMake and Ninja
   - Target architecture: arm64-v8a

2. **libappbuilder.so Build**
   - Uses Android NDK's ndk-build tool
   - Target architecture: arm64-v8a
   - Build configuration: `make/Android.mk` and `make/Application.mk`

3. **GenieAPIService Build**
   - Depends on libappbuilder.so
   - Uses Android NDK's ndk-build tool
   - Build configuration: `scripts/Android.mk` and `scripts/Application.mk`

4. **Android APK Build**
   - Uses Gradle build system
   - Packages all native libraries
   - Configuration: `Android/app/build.gradle.kts`

5. **Library Collection**
   - Copies runtime libraries from QNN SDK
   - Copies generated libraries from build output
   - Consolidates all files to `build_android/output/libs/arm64-v8a/`


## Version Information

- **Script Version**: 2.0
- **Supported NDK Version**: r26d
- **Supported Target Architecture**: arm64-v8a
- **Supported QAIRT Version**: 2.42.0.251225
- **Gradle Version**: 8.7+
- **Android Gradle Plugin**: 8.7.3+

## License

This script follows the same license as the ai-engine-direct-helper project (BSD-3-Clause).

## Feedback and Support

If you encounter issues:

1. Check the "Troubleshooting" section in this document
2. Verify your environment configuration is correct
3. Review error messages in the build log

---

**Last Updated**: 2026-04-17
