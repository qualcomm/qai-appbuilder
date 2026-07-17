@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem =============================================================================
rem
rem Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
rem
rem SPDX-License-Identifier: BSD-3-Clause
rem
rem =============================================================================

set "SCRIPT_DIR=%~dp0"
set "EXTERNAL_DIR=%SCRIPT_DIR%..\External"
set "ANDROID_PROJECT_DIR=%SCRIPT_DIR%..\Android"
set "BUILD_DIR=%SCRIPT_DIR%build-android"
set "OUTPUT_DIR=%BUILD_DIR%\output\libs\arm64-v8a"
set "ANDROID_ABI=arm64-v8a"
rem libappbuilder's SVC helper (SharedRegion.hpp/SvcProcess.hpp) calls
rem ASharedMemory_create (API 26) and posix_spawnp (API 28); android-21 makes
rem those declarations unavailable at compile time, so this must be >= 28.
set "ANDROID_PLATFORM=android-28"
if "%QNN_STUB_VERSION%"=="" set "QNN_STUB_VERSION=v79;v81"
if "%JOBS%"=="" set "JOBS=4"

if "%QNN_SDK_ROOT%"=="" (
    echo [ERROR] QNN_SDK_ROOT is not set.
    echo         Example: set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.x.x.x\
    exit /b 1
)

if "%NDK_ROOT%"=="" (
    if not "%ANDROID_NDK_ROOT%"=="" set "NDK_ROOT=%ANDROID_NDK_ROOT%"
)
if "%ANDROID_NDK_ROOT%"=="" (
    if not "%NDK_ROOT%"=="" set "ANDROID_NDK_ROOT=%NDK_ROOT%"
)
if "%NDK_ROOT%"=="" (
    echo [ERROR] NDK_ROOT or ANDROID_NDK_ROOT is not set.
    echo         Example: set NDK_ROOT=C:\Programs\android-ndk-r26d
    exit /b 1
)

set "NDK_BUILD=%NDK_ROOT%\ndk-build.cmd"
if not exist "%NDK_BUILD%" set "NDK_BUILD=%NDK_ROOT%\ndk-build.bat"
if not exist "%NDK_BUILD%" set "NDK_BUILD=%NDK_ROOT%\ndk-build"
if not exist "%NDK_BUILD%" (
    echo [ERROR] ndk-build was not found under NDK_ROOT: %NDK_ROOT%
    exit /b 1
)

where ninja.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ninja.exe was not found in PATH.
    exit /b 1
)

where cmake.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] cmake.exe was not found in PATH.
    exit /b 1
)

if not exist "%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so" (
    echo [ERROR] QNN Android runtime was not found: %QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so
    exit /b 1
)

set "ANDROID_TOOLCHAIN_FILE=%NDK_ROOT%\build\cmake\android.toolchain.cmake"
if not exist "%ANDROID_TOOLCHAIN_FILE%" (
    echo [ERROR] Android CMake toolchain file was not found: %ANDROID_TOOLCHAIN_FILE%
    exit /b 1
)

set "GRADLEW=%ANDROID_PROJECT_DIR%\gradlew.bat"
if not exist "%GRADLEW%" (
    echo [ERROR] gradlew.bat was not found under: %ANDROID_PROJECT_DIR%
    exit /b 1
)

if "%JAVA_HOME%"=="" (
    where java.exe >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] JAVA_HOME is not set and java was not found in PATH.
        echo         Example: set JAVA_HOME=C:\Program Files\Java\jdk-17
        exit /b 1
    )
)

echo ============================================================
echo  GenieAPIService - Android native build
echo ============================================================
echo   QNN_SDK_ROOT     : %QNN_SDK_ROOT%
echo   NDK_ROOT         : %NDK_ROOT%
echo   QNN_STUB_VERSION : %QNN_STUB_VERSION%
echo   JOBS             : %JOBS%
echo   OUTPUT_DIR       : %OUTPUT_DIR%
echo ============================================================

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set "SAMPLERATE_BUILD=%BUILD_DIR%\libsamplerate"
cmake -S "%EXTERNAL_DIR%\libsamplerate" ^
      -B "%SAMPLERATE_BUILD%" ^
      -G Ninja ^
      -DCMAKE_TOOLCHAIN_FILE="%ANDROID_TOOLCHAIN_FILE%" ^
      -DANDROID_ABI=%ANDROID_ABI% ^
      -DANDROID_PLATFORM=%ANDROID_PLATFORM% ^
      -DCMAKE_BUILD_TYPE=Release ^
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON ^
      -DBUILD_SHARED_LIBS=ON ^
      -DLIBSAMPLERATE_EXAMPLES=OFF ^
      -DBUILD_TESTING=OFF
if errorlevel 1 exit /b 1

cmake --build "%SAMPLERATE_BUILD%" --config Release --parallel %JOBS%
if errorlevel 1 exit /b 1

rem A literal (non-wildcard) name in "for /r ... in (name)" is emitted for every
rem scanned directory regardless of whether the file actually exists there, so
rem the existence check below would never trip; using a wildcard makes cmd.exe
rem only match files that are actually present.
set "SAMPLERATE_SO="
for /r "%SAMPLERATE_BUILD%" %%F in (*libsamplerate.so) do set "SAMPLERATE_SO=%%F"
if "%SAMPLERATE_SO%"=="" (
    echo [ERROR] libsamplerate.so was not produced.
    exit /b 1
)
copy /Y "%SAMPLERATE_SO%" "%BUILD_DIR%\libsamplerate.so" >nul

set "APPBUILDER_ROOT=%SCRIPT_DIR%..\..\..\.."
set "APPBUILDER_BUILD=%BUILD_DIR%\libappbuilder"
cmake -S "%APPBUILDER_ROOT%" ^
      -B "%APPBUILDER_BUILD%" ^
      -G Ninja ^
      -DCMAKE_TOOLCHAIN_FILE="%ANDROID_TOOLCHAIN_FILE%" ^
      -DANDROID_ABI=%ANDROID_ABI% ^
      -DANDROID_PLATFORM=%ANDROID_PLATFORM% ^
      -DCMAKE_BUILD_TYPE=Release ^
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON
if errorlevel 1 exit /b 1

cmake --build "%APPBUILDER_BUILD%" --config Release --parallel %JOBS%
if errorlevel 1 exit /b 1

rem libappbuilder's own CMakeLists.txt overrides LIBRARY_OUTPUT_PATH to
rem <APPBUILDER_ROOT>/lib, not anywhere under the out-of-tree build dir, so the
rem search base must be the source-tree lib dir (see same wildcard note above).
set "APPBUILDER_SO="
for /r "%APPBUILDER_ROOT%\lib" %%F in (*libappbuilder.so) do set "APPBUILDER_SO=%%F"
if "%APPBUILDER_SO%"=="" (
    echo [ERROR] libappbuilder.so was not produced.
    exit /b 1
)
copy /Y "%APPBUILDER_SO%" "%BUILD_DIR%\libappbuilder.so" >nul

pushd "%SCRIPT_DIR%"
rem `call` is required: ndk-build.cmd is itself a batch script, and invoking it
rem without `call` transfers control into it permanently instead of returning
rem here afterward, silently skipping every command below (including the final
rem packaging/copy steps) even though the overall process still exits 0.
call "%NDK_BUILD%" NDK_PROJECT_PATH=. NDK_APPLICATION_MK=scripts\Application.mk APP_BUILD_SCRIPT=scripts\Android.mk "NDK_OUT=%BUILD_DIR%\obj" "NDK_LIBS_OUT=%BUILD_DIR%\libs" -j%JOBS%
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "%BUILD_RESULT%"=="0" exit /b %BUILD_RESULT%

copy /Y "%QNN_SDK_ROOT%\lib\aarch64-android\libGenie.so" "%OUTPUT_DIR%\" >nul
copy /Y "%QNN_SDK_ROOT%\lib\aarch64-android\libQnnHtp*.so" "%OUTPUT_DIR%\" >nul
copy /Y "%QNN_SDK_ROOT%\lib\aarch64-android\libQnnSystem.so" "%OUTPUT_DIR%\" >nul
rem QNN_STUB_VERSION is a ';'-separated list (e.g. "v79;v81"); copy each
rem architecture's Stub/Skel/.cat runtime files in turn.
for %%v in (%QNN_STUB_VERSION:;= %) do (
    copy /Y "%QNN_SDK_ROOT%\lib\aarch64-android\libQnnHtp%%vStub.so" "%OUTPUT_DIR%\" >nul 2>nul
    if exist "%QNN_SDK_ROOT%\lib\hexagon-%%v\unsigned" (
        copy /Y "%QNN_SDK_ROOT%\lib\hexagon-%%v\unsigned\libQnnHtp%%vSkel.so" "%OUTPUT_DIR%\" >nul 2>nul
        copy /Y "%QNN_SDK_ROOT%\lib\hexagon-%%v\unsigned\libqnnhtp%%v.cat" "%OUTPUT_DIR%\" >nul 2>nul
    )
)
copy /Y "%BUILD_DIR%\libappbuilder.so" "%OUTPUT_DIR%\" >nul
copy /Y "%BUILD_DIR%\libsamplerate.so" "%OUTPUT_DIR%\" >nul
copy /Y "%BUILD_DIR%\libs\arm64-v8a\*.so" "%OUTPUT_DIR%\" >nul 2>nul
copy /Y "%BUILD_DIR%\obj\local\arm64-v8a\*.so" "%OUTPUT_DIR%\" >nul 2>nul

rem QAI_APP_BUILDER_VERSION must stay in sync with scripts\version.cmake.
(
    echo QAI_APP_BUILDER_VERSION: 2.3.7
    echo QNN_SDK_ROOT: %QNN_SDK_ROOT%
    echo QNN_STUB_VERSION: %QNN_STUB_VERSION%
) > "%BUILD_DIR%\output\version"

pushd "%ANDROID_PROJECT_DIR%"
call "%GRADLEW%" assembleRelease "-PqnnStubVersion=%QNN_STUB_VERSION%"
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "%BUILD_RESULT%"=="0" exit /b %BUILD_RESULT%

set "APK_OUT="
for /r "%ANDROID_PROJECT_DIR%\app\build\outputs\apk\release" %%F in (*.apk) do set "APK_OUT=%%F"
if "%APK_OUT%"=="" (
    echo [ERROR] Gradle finished but no .apk was produced under app\build\outputs\apk\release.
    exit /b 1
)
set "APK_DEST=%BUILD_DIR%\output\GenieAPIService.apk"
copy /Y "%APK_OUT%" "%APK_DEST%" >nul

echo.
echo ============================================================
echo  Android build complete.
echo  Native libs : %OUTPUT_DIR%
echo  APK         : %APK_DEST%
echo ============================================================

endlocal
