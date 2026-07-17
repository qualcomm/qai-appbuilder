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

rem Gradle distribution resolution, in priority order:
rem   1. GRADLE_HOME - an already-extracted Gradle install; call its own
rem      bin\gradle.bat directly, bypassing the wrapper (and GRADLE_DISTRIBUTION_ZIP)
rem      entirely.
rem   2. GRADLE_DISTRIBUTION_ZIP - internal override: rewrites distributionUrl to
rem      a local zip instead of downloading from services.gradle.org.
rem   3. neither set - the wrapper downloads as usual, but GRADLE_USER_HOME is
rem      redirected under build-android so the download stays self-contained
rem      instead of landing in the user's global ~/.gradle.
set "GRADLE_WRAPPER_PROPS=%ANDROID_PROJECT_DIR%\gradle\wrapper\gradle-wrapper.properties"
set "GRADLE_EXE=%GRADLEW%"
set "GRADLE_MODE=wrapper, auto-download"
if not "%GRADLE_HOME%"=="" (
    if not exist "%GRADLE_HOME%\bin\gradle.bat" (
        echo [ERROR] GRADLE_HOME does not contain bin\gradle.bat: %GRADLE_HOME%
        exit /b 1
    )
    set "GRADLE_EXE=%GRADLE_HOME%\bin\gradle.bat"
    set "GRADLE_MODE=GRADLE_HOME: %GRADLE_HOME%"
) else if not "%GRADLE_DISTRIBUTION_ZIP%"=="" (
    if not exist "%GRADLE_DISTRIBUTION_ZIP%" (
        echo [ERROR] GRADLE_DISTRIBUTION_ZIP does not exist: %GRADLE_DISTRIBUTION_ZIP%
        exit /b 1
    )
    if not exist "%GRADLE_WRAPPER_PROPS%" (
        echo [ERROR] gradle-wrapper.properties was not found under: %ANDROID_PROJECT_DIR%\gradle\wrapper
        exit /b 1
    )
    set "GRADLE_LOCAL_URL=%GRADLE_DISTRIBUTION_ZIP:\=/%"
    findstr /v /b /c:"distributionUrl=" "%GRADLE_WRAPPER_PROPS%" > "%GRADLE_WRAPPER_PROPS%.tmp"
    echo distributionUrl=file\:///!GRADLE_LOCAL_URL!>> "%GRADLE_WRAPPER_PROPS%.tmp"
    move /Y "%GRADLE_WRAPPER_PROPS%.tmp" "%GRADLE_WRAPPER_PROPS%" >nul
    set "GRADLE_MODE=local zip: %GRADLE_DISTRIBUTION_ZIP%"
) else (
    set "GRADLE_USER_HOME=%BUILD_DIR%\gradle-home"
    set "GRADLE_MODE=wrapper, auto-download into !GRADLE_USER_HOME!"
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
echo   GRADLE           : %GRADLE_MODE%
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
set "APPBUILDER_NDK_OUT=%BUILD_DIR%\libappbuilder-obj"
set "APPBUILDER_LIBS_OUT=%BUILD_DIR%\libappbuilder-libs"
rem Mirrors the repository's own top-level Makefile "android" target (plain
rem ndk-build against make\Android.mk/Application.mk), which is the documented
rem way to build libappbuilder.so for Android; not a CMake+Ninja build of src\.
pushd "%APPBUILDER_ROOT%"
call "%NDK_BUILD%" NDK_PROJECT_PATH=. NDK_APPLICATION_MK=make\Application.mk APP_BUILD_SCRIPT=make\Android.mk "NDK_OUT=%APPBUILDER_NDK_OUT%" "NDK_LIBS_OUT=%APPBUILDER_LIBS_OUT%" -j%JOBS%
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "%BUILD_RESULT%"=="0" exit /b %BUILD_RESULT%

set "APPBUILDER_SO=%APPBUILDER_LIBS_OUT%\arm64-v8a\libappbuilder.so"
if not exist "%APPBUILDER_SO%" (
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
call "%GRADLE_EXE%" assembleRelease "-PqnnStubVersion=%QNN_STUB_VERSION%"
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
