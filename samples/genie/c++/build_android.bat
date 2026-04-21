@echo off
REM =============================================================================
REM Automated build script for libappbuilder.so and GenieAPIService (Android)
REM =============================================================================

setlocal enabledelayedexpansion

echo ============================================================================
echo Android Build Script for libappbuilder and GenieAPIService
echo ============================================================================
echo.

REM =============================================================================
REM Configuration Section - Modify these paths according to your environment
REM =============================================================================

REM Set QNN SDK Root (Qualcomm AI Runtime SDK)
set "QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225\"

REM Set Android NDK Root
set "NDK_ROOT=C:\work\android-ndk-r26d-windows\android-ndk-r26d\" 

REM Set Android NDK Root (Windows path format)
set "ANDROID_NDK_ROOT=%NDK_ROOT%"

REM Add NDK tools to PATH
set "PATH=%PATH%;%NDK_ROOT%toolchains\llvm\prebuilt\windows-x86_64\bin"

REM Number of parallel jobs
set "JOBS=4"

REM =============================================================================
REM Validate Environment
REM =============================================================================

echo [1/9] Validating environment...
echo.

if not exist "%QNN_SDK_ROOT%" (
    echo ERROR: QNN_SDK_ROOT not found at: %QNN_SDK_ROOT%
    echo Please update the QNN_SDK_ROOT variable in this script.
    exit /b 1
)

if not exist "%NDK_ROOT%" (
    echo ERROR: NDK_ROOT not found at: %NDK_ROOT%
    echo Please update the NDK_ROOT variable in this script.
    exit /b 1
)

set "NDK_MAKE=%NDK_ROOT%prebuilt\windows-x86_64\bin\make.exe"
if not exist "%NDK_MAKE%" (
    echo ERROR: NDK make not found at: %NDK_MAKE%
    exit /b 1
)

echo Environment validated successfully.
echo   QNN_SDK_ROOT: %QNN_SDK_ROOT%
echo   NDK_ROOT: %NDK_ROOT%
echo   NDK_MAKE: %NDK_MAKE%
echo.

REM =============================================================================
REM Setup Build Directory Structure
REM =============================================================================

echo [2/9] Setting up build directory structure...
echo.

set "BUILD_ROOT=%~dp0build_android"
set "BUILD_APPBUILDER=%BUILD_ROOT%\appbuilder"
set "BUILD_SERVICE=%BUILD_ROOT%\service"
set "BUILD_OUTPUT=%BUILD_ROOT%\output"
set "BUILD_OUTPUT_LIBS=%BUILD_OUTPUT%\libs\arm64-v8a"

REM Create build directories
if not exist "%BUILD_ROOT%" mkdir "%BUILD_ROOT%"
if not exist "%BUILD_APPBUILDER%" mkdir "%BUILD_APPBUILDER%"
if not exist "%BUILD_SERVICE%" mkdir "%BUILD_SERVICE%"
if not exist "%BUILD_OUTPUT%" mkdir "%BUILD_OUTPUT%"
if not exist "%BUILD_OUTPUT_LIBS%" mkdir "%BUILD_OUTPUT_LIBS%"

echo Build directories created:
echo   BUILD_ROOT: %BUILD_ROOT%
echo   BUILD_APPBUILDER: %BUILD_APPBUILDER%
echo   BUILD_SERVICE: %BUILD_SERVICE%
echo   BUILD_OUTPUT: %BUILD_OUTPUT%
echo.

REM =============================================================================
REM Build libsamplerate.so
REM =============================================================================

echo [3/9] Building libsamplerate.so...
echo.

set "LIBSAMPLERATE_DIR=%~dp0External\libsamplerate"
set "LIBSAMPLERATE_BUILD=%BUILD_ROOT%\libsamplerate_build"

if not exist "%LIBSAMPLERATE_BUILD%" mkdir "%LIBSAMPLERATE_BUILD%"

cd /d "%LIBSAMPLERATE_BUILD%"

echo Configuring libsamplerate with CMake...
cmake -G "Ninja" ^
      -DCMAKE_TOOLCHAIN_FILE="%NDK_ROOT%build\cmake\android.toolchain.cmake" ^
      -DANDROID_ABI=arm64-v8a ^
      -DANDROID_PLATFORM=android-21 ^
      -DCMAKE_BUILD_TYPE=Release ^
      -DBUILD_SHARED_LIBS=ON ^
      -DLIBSAMPLERATE_EXAMPLES=OFF ^
      -DLIBSAMPLERATE_INSTALL=OFF ^
      -DBUILD_TESTING=OFF ^
      "%LIBSAMPLERATE_DIR%"

if errorlevel 1 (
    echo ERROR: libsamplerate CMake configuration failed!
    exit /b 1
)

echo Building libsamplerate...
cmake --build . --config Release

if errorlevel 1 (
    echo ERROR: libsamplerate build failed!
    exit /b 1
)

echo libsamplerate.so build succeeded.
echo.

REM =============================================================================
REM Skip libcurl.so (not actually used by GenieAPIService)
REM =============================================================================

echo [4/9] Skipping libcurl.so (not required by GenieAPIService)...
echo.
echo Note: libcurl is defined in Android.mk but not linked by GenieAPIService.
echo Creating placeholder to satisfy build system requirements.
echo.

REM =============================================================================
REM Build libappbuilder.so
REM =============================================================================

echo [5/9] Building libappbuilder.so...
echo.

REM Navigate to project root (3 levels up from samples/genie/c++)
cd /d "%~dp0..\..\..\"

REM Clean previous build artifacts in source directory
if exist "libs" rmdir /s /q "libs"
if exist "obj" rmdir /s /q "obj"

echo Running: "%NDK_MAKE%" android
"%NDK_MAKE%" android
if errorlevel 1 (
    echo ERROR: libappbuilder.so build failed!
    exit /b 1
)

echo.
echo libappbuilder.so build succeeded.
echo.

REM =============================================================================
REM Copy dependencies to Service directory
REM =============================================================================

echo [6/9] Copying dependencies to Service directory...
echo.

REM Copy libsamplerate.so
echo Copying libsamplerate.so...
if exist "%LIBSAMPLERATE_BUILD%\src\libsamplerate.so" (
    copy "%LIBSAMPLERATE_BUILD%\src\libsamplerate.so" "samples\genie\c++\Service\libsamplerate.so" /Y
) else if exist "%LIBSAMPLERATE_BUILD%\libsamplerate.so" (
    copy "%LIBSAMPLERATE_BUILD%\libsamplerate.so" "samples\genie\c++\Service\libsamplerate.so" /Y
) else (
    echo ERROR: libsamplerate.so not found in build directory
    exit /b 1
)

REM Create placeholder libcurl.so (required by Android.mk but not linked)
echo Creating placeholder libcurl.so...
echo. > "samples\genie\c++\Service\libcurl.so"
echo Placeholder libcurl.so created.

REM Copy libappbuilder.so
echo Copying libappbuilder.so...
if not exist "libs\arm64-v8a\libappbuilder.so" (
    echo ERROR: libappbuilder.so not found at expected location: libs\arm64-v8a\libappbuilder.so
    exit /b 1
)

REM Copy to build directory
copy "libs\arm64-v8a\libappbuilder.so" "%BUILD_APPBUILDER%\libappbuilder.so" /Y
if errorlevel 1 (
    echo ERROR: Failed to copy libappbuilder.so to build directory
    exit /b 1
)

REM Copy to Service directory (required for GenieAPIService build)
copy "libs\arm64-v8a\libappbuilder.so" "samples\genie\c++\Service\libappbuilder.so" /Y
if errorlevel 1 (
    echo ERROR: Failed to copy libappbuilder.so to Service directory
    exit /b 1
)

echo All dependencies copied successfully.
echo.

REM Move build artifacts to build directory
echo Moving appbuilder build artifacts to build directory...
move "libs" "%BUILD_APPBUILDER%\libs" >nul 2>&1
move "obj" "%BUILD_APPBUILDER%\obj" >nul 2>&1
echo.

REM =============================================================================
REM Build GenieAPIService
REM =============================================================================

echo [7/9] Building GenieAPIService...
echo.

cd /d "%~dp0Service"

REM Clean previous build artifacts in source directory
if exist "libs" rmdir /s /q "libs"
if exist "obj" rmdir /s /q "obj"

echo Running: "%NDK_MAKE%" android -j%JOBS%
"%NDK_MAKE%" android -j%JOBS%
if errorlevel 1 (
    echo ERROR: GenieAPIService build failed!
    exit /b 1
)

echo.
echo GenieAPIService build succeeded.
echo.

REM =============================================================================
REM Copy all required libraries to output directory
REM =============================================================================

echo [8/9] Copying required libraries to output directory...
echo.

REM Copy only the required QNN SDK libraries
echo Copying required QNN SDK libraries...
set "QNN_LIB_DIR=%QNN_SDK_ROOT%lib\aarch64-android"
set "QNN_HEXAGON_V79_DIR=%QNN_SDK_ROOT%lib\hexagon-v79\unsigned"
set "QNN_HEXAGON_V81_DIR=%QNN_SDK_ROOT%lib\hexagon-v81\unsigned"

REM Copy libraries from aarch64-android directory
set "ANDROID_LIBS=libGenie.so libQnnHtp.so libQnnHtpNetRunExtensions.so libQnnHtpPrepare.so libQnnHtpV79Stub.so libQnnHtpV81Stub.so libQnnSystem.so"

for %%L in (%ANDROID_LIBS%) do (
    if exist "%QNN_LIB_DIR%\%%L" (
        copy "%QNN_LIB_DIR%\%%L" "%BUILD_OUTPUT_LIBS%\" /Y >nul
        echo   Copied %%L ^(from aarch64-android^)
    )
)

REM Copy V79 Hexagon libraries
if exist "%QNN_HEXAGON_V79_DIR%\libQnnHtpV79.so" (
    copy "%QNN_HEXAGON_V79_DIR%\libQnnHtpV79.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libQnnHtpV79.so ^(from hexagon-v79^)
)

if exist "%QNN_HEXAGON_V79_DIR%\libQnnHtpV79Skel.so" (
    copy "%QNN_HEXAGON_V79_DIR%\libQnnHtpV79Skel.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libQnnHtpV79Skel.so ^(from hexagon-v79^)
)

REM Copy V81 Hexagon libraries
if exist "%QNN_HEXAGON_V81_DIR%\libQnnHtpV81.so" (
    copy "%QNN_HEXAGON_V81_DIR%\libQnnHtpV81.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libQnnHtpV81.so ^(from hexagon-v81^)
)

if exist "%QNN_HEXAGON_V81_DIR%\libQnnHtpV81Skel.so" (
    copy "%QNN_HEXAGON_V81_DIR%\libQnnHtpV81Skel.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libQnnHtpV81Skel.so ^(from hexagon-v81^)
)

echo Required QNN SDK libraries copied.
echo.

REM Copy GenieAPIService built libraries
echo Copying GenieAPIService built libraries...
if exist "obj\local\arm64-v8a\libGenieAPIService.so" (
    copy "obj\local\arm64-v8a\libGenieAPIService.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libGenieAPIService.so
) else (
    echo ERROR: libGenieAPIService.so not found
    exit /b 1
)

if exist "obj\local\arm64-v8a\libJNIGenieAPIService.so" (
    copy "obj\local\arm64-v8a\libJNIGenieAPIService.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libJNIGenieAPIService.so
) else (
    echo ERROR: libJNIGenieAPIService.so not found
    exit /b 1
)

echo GenieAPIService libraries copied.
echo.

REM Copy libappbuilder.so
echo Copying libappbuilder.so...
if exist "%BUILD_APPBUILDER%\libappbuilder.so" (
    copy "%BUILD_APPBUILDER%\libappbuilder.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libappbuilder.so
) else (
    echo ERROR: libappbuilder.so not found
    exit /b 1
)

REM Copy libsamplerate.so
echo Copying libsamplerate.so...
if exist "%LIBSAMPLERATE_BUILD%\src\libsamplerate.so" (
    copy "%LIBSAMPLERATE_BUILD%\src\libsamplerate.so" "%BUILD_OUTPUT_LIBS%\" /Y >nul
    echo   Copied libsamplerate.so
) else (
    echo ERROR: libsamplerate.so not found
    exit /b 1
)

REM Create placeholder libcurl.so in output (for reference)
echo Creating placeholder libcurl.so in output...
echo. > "%BUILD_OUTPUT_LIBS%\libcurl.so"
echo   Created libcurl.so (placeholder)

echo.
echo All required libraries copied successfully.

echo.

REM Move Service build artifacts to build directory
echo Moving Service build artifacts to build directory...
if exist "libs" move "libs" "%BUILD_SERVICE%\libs" >nul 2>&1
if exist "obj" move "obj" "%BUILD_SERVICE%\obj" >nul 2>&1

REM Clean up the temporary dependency files in Service directory
if exist "libappbuilder.so" del "libappbuilder.so" >nul 2>&1
if exist "libsamplerate.so" del "libsamplerate.so" >nul 2>&1
if exist "libcurl.so" del "libcurl.so" >nul 2>&1

echo.

REM =============================================================================
REM Build Android APK
REM =============================================================================

echo [9/10] Building Android APK...
echo.

cd /d "%~dp0Android"

echo Checking Java version...
java -version 2>&1 | findstr /C:"25.0" >nul
if not errorlevel 1 (
    echo.
    echo WARNING: Java 25 detected. Gradle 8.5 requires Java 17 or 21.
    echo Attempting to download and use portable Java 17...
    echo.
    
    set "PORTABLE_JAVA=%~dp0build_android\jdk-17"
    if not exist "!PORTABLE_JAVA!\bin\java.exe" (
        echo Downloading Microsoft OpenJDK 17 ^(~180 MB^)...
        echo This is a one-time download, please wait...
        echo.
        
        powershell -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri 'https://aka.ms/download-jdk/microsoft-jdk-17.0.12-windows-x64.zip' -OutFile '%~dp0build_android\jdk-17.zip' -UseBasicParsing; Write-Host 'Download completed.'; Expand-Archive -Path '%~dp0build_android\jdk-17.zip' -DestinationPath '%~dp0build_android\jdk-temp' -Force; $jdkDir = Get-ChildItem -Path '%~dp0build_android\jdk-temp' -Directory | Select-Object -First 1; Move-Item -Path $jdkDir.FullName -Destination '%~dp0build_android\jdk-17' -Force; Remove-Item -Path '%~dp0build_android\jdk-temp' -Recurse -Force; Remove-Item -Path '%~dp0build_android\jdk-17.zip' -Force; Write-Host 'Java 17 installed successfully.' } catch { Write-Host 'Error:' $_.Exception.Message; exit 1 }"
        
        if errorlevel 1 (
            echo.
            echo ERROR: Failed to download/extract Java 17.
            echo Native libraries are ready in build_android/output/
            echo.
            echo Please install Java 17 manually and run the script again.
            echo.
            cd /d "%~dp0"
            goto :skip_apk
        )
    )
    
    if exist "!PORTABLE_JAVA!\bin\java.exe" (
        echo Using portable Java 17 from: !PORTABLE_JAVA!
        set "JAVA_HOME=!PORTABLE_JAVA!"
        set "PATH=!PORTABLE_JAVA!\bin;!PATH!"
        echo.
    ) else (
        echo ERROR: Java 17 installation failed.
        echo Native libraries are ready in build_android/output/
        echo.
        cd /d "%~dp0"
        goto :skip_apk
    )
)

echo Running Gradle build ^(release mode^)...
echo Stopping any existing Gradle daemons...
call gradlew.bat --stop >nul 2>&1
echo.
echo Cleaning previous build...
call gradlew.bat clean --no-daemon
if errorlevel 1 (
    echo WARNING: Gradle clean failed, continuing anyway...
)
echo.
echo Building release APK...
call gradlew.bat assembleRelease --no-daemon
if errorlevel 1 (
    echo.
    echo WARNING: Android APK build failed!
    echo Native libraries are ready in build_android/output/
    echo.
    echo Note: Gradle build failed. Please check the error messages above.
    echo.
    echo If the build failed due to network issues, you may need to configure proxy:
    echo   set HTTP_PROXY=http://your-proxy-host:port
    echo   set HTTPS_PROXY=http://your-proxy-host:port
    echo   set GRADLE_OPTS=-Dhttp.proxyHost=your-proxy-host -Dhttp.proxyPort=port -Dhttps.proxyHost=your-proxy-host -Dhttps.proxyPort=port
    echo.
    echo Then run the script again.
    echo.
    cd /d "%~dp0"
    goto :skip_apk
)

echo.
echo Android APK build succeeded.
echo.

REM Copy APK to output directory
set "APK_OUTPUT=%BUILD_OUTPUT%\apk"
if not exist "%APK_OUTPUT%" mkdir "%APK_OUTPUT%"

if exist "app\build\outputs\apk\debug\app-debug.apk" (
    copy "app\build\outputs\apk\debug\app-debug.apk" "%APK_OUTPUT%\GenieAPIService-debug.apk" /Y
    echo APK copied to: %APK_OUTPUT%\GenieAPIService-debug.apk
) else if exist "app\build\outputs\apk\release\app-release.apk" (
    copy "app\build\outputs\apk\release\app-release.apk" "%APK_OUTPUT%\GenieAPIService.apk" /Y
    echo APK copied to: %APK_OUTPUT%\GenieAPIService.apk
) else (
    echo WARNING: APK not found at expected location
)

echo.

:skip_apk

REM =============================================================================
REM Clean up generated files
REM =============================================================================

echo.
echo Cleaning up generated files...
echo.

REM Clean Android project generated files
cd /d "%~dp0Android"
if exist ".gradle" (
    echo Removing Android/.gradle...
    rmdir /s /q ".gradle" >nul 2>&1
)
if exist "app\.cxx" (
    echo Removing Android/app/.cxx...
    rmdir /s /q "app\.cxx" >nul 2>&1
)
if exist "app\build" (
    echo Removing Android/app/build...
    rmdir /s /q "app\build" >nul 2>&1
)
if exist "app\libs" (
    echo Removing Android/app/libs...
    rmdir /s /q "app\libs" >nul 2>&1
)
if exist "build" (
    echo Removing Android/build...
    rmdir /s /q "build" >nul 2>&1
)

REM Clean Service generated files
cd /d "%~dp0Service"
if exist "libs" (
    echo Removing Service/libs...
    rmdir /s /q "libs" >nul 2>&1
)
if exist "obj" (
    echo Removing Service/obj...
    rmdir /s /q "obj" >nul 2>&1
)
if exist "libappbuilder.so" (
    echo Removing Service/libappbuilder.so...
    del "libappbuilder.so" >nul 2>&1
)
if exist "libsamplerate.so" (
    echo Removing Service/libsamplerate.so...
    del "libsamplerate.so" >nul 2>&1
)
if exist "libcurl.so" (
    echo Removing Service/libcurl.so...
    del "libcurl.so" >nul 2>&1
)

REM Clean project root generated files (3 levels up from samples/genie/c++)
cd /d "%~dp0..\..\..\"
if exist "libs" (
    echo Removing project root libs...
    rmdir /s /q "libs" >nul 2>&1
)
if exist "obj" (
    echo Removing project root obj...
    rmdir /s /q "obj" >nul 2>&1
)

echo Generated files cleaned up.
echo.

REM =============================================================================
REM Build Summary
REM =============================================================================

echo [10/10] Build completed successfully!
echo.
echo ============================================================================
echo Build Summary
echo ============================================================================
echo.
echo All build artifacts have been organized in the build directory:
echo   %BUILD_ROOT%
echo.
echo Directory structure:
echo   build_android\
echo   ├── appbuilder\          - libappbuilder.so build artifacts
echo   ├── service\             - GenieAPIService build artifacts
echo   └── output\
echo       └── libs\
echo           └── arm64-v8a\   - All required .so files for deployment
echo.
echo Output libraries location:
echo   %BUILD_OUTPUT_LIBS%
echo.
echo Libraries in output directory:

if exist "%BUILD_OUTPUT_LIBS%\*.so" (
    dir /b "%BUILD_OUTPUT_LIBS%\*.so"
) else (
    echo   (No .so files found)
)

echo.
echo ============================================================================
echo Build Output:
echo ============================================================================
echo Native Libraries (.so files):
echo   %BUILD_OUTPUT_LIBS%
echo.
echo Android APK:
echo   %BUILD_OUTPUT%\apk\GenieAPIService.apk
echo ============================================================================
echo.

cd /d "%~dp0"

endlocal
exit /b 0
