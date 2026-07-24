@echo off
setlocal enabledelayedexpansion

rem =============================================================================
rem
rem Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
rem
rem SPDX-License-Identifier: BSD-3-Clause
rem
rem =============================================================================
rem
rem One-click bootstrap + build script for GenieAPIService on Windows (ARM64).
rem Assumes a brand-new machine: no Visual Studio, no CMake. This script
rem detects what is missing, installs it locally and idempotently, then
rem configures and builds the CMake project. Running it again on a machine
rem that already has everything is a no-op for the toolchain and just
rem reconfigures/rebuilds.
rem
rem Required environment variables:
rem   QNN_SDK_ROOT   Path to the Qualcomm AI Runtime (QAIRT) SDK root.
rem
rem Optional environment variables (with defaults):
rem   QNN_STUB_VERSION   Hexagon DSP stub version(s), ';'-separated (default: v73;v81)
rem   BUILD_TYPE         CMake build type                            (default: Release)
rem   BUILD_AS_DLL       Build only the GenieAPILibrary .dll, excluding the exe
rem                      (default: OFF, which builds exe + dll together)
rem
rem Installing Visual Studio Build Tools requires an elevated ("Run as
rem Administrator") command prompt; this script only asks for elevation when
rem an actual install is about to happen, never otherwise.
rem
rem Usage:
rem   set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.x.x.x
rem   build_win.bat
rem =============================================================================

rem ── Defaults ──────────────────────────────────────────────────────────────────
if not defined QNN_STUB_VERSION set "QNN_STUB_VERSION=v73;v81"
if not defined BUILD_TYPE set "BUILD_TYPE=Release"
if not defined BUILD_AS_DLL set "BUILD_AS_DLL=OFF"

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "BUILD_DIR=%SCRIPT_DIR%\build"
set "TOOLCHAIN_DIR=%SCRIPT_DIR%\.toolchain"
set "CMAKE_VERSION=3.31.5"

rem ── Admin check ───────────────────────────────────────────────────────────────
rem Compute the "needs install" flags first, so a machine that already has
rem everything installed can run this script from a normal, non-elevated prompt.
set "NEED_VS_INSTALL=0"
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS_INSTALL_PATH="
if exist "%VSWHERE%" for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Workload.VCTools Microsoft.VisualStudio.Component.VC.Tools.ARM64 -property installationPath`) do set "VS_INSTALL_PATH=%%I"
if not defined VS_INSTALL_PATH set "NEED_VS_INSTALL=1"

set "NEED_CMAKE_INSTALL=0"
set "CMAKE_CURRENT_VERSION="
cmake --version >nul 2>&1
if errorlevel 1 set "NEED_CMAKE_INSTALL=1"
if not "%NEED_CMAKE_INSTALL%"=="1" for /f "tokens=3" %%V in ('cmake --version ^| findstr /b /c:"cmake version"') do set "CMAKE_CURRENT_VERSION=%%V"
if defined CMAKE_CURRENT_VERSION for /f "usebackq tokens=1-2 delims=." %%a in ('%CMAKE_CURRENT_VERSION%') do (set "CMAKE_VER_MAJOR=%%a" & set "CMAKE_VER_MINOR=%%b")
if defined CMAKE_CURRENT_VERSION if %CMAKE_VER_MAJOR% LSS 3 set "NEED_CMAKE_INSTALL=1"
if defined CMAKE_CURRENT_VERSION if %CMAKE_VER_MAJOR%==3 if %CMAKE_VER_MINOR% LSS 25 set "NEED_CMAKE_INSTALL=1"

set "NEED_INSTALL=0"
if "%NEED_VS_INSTALL%"=="1" set "NEED_INSTALL=1"
if "%NEED_CMAKE_INSTALL%"=="1" set "NEED_INSTALL=1"

if "%NEED_INSTALL%"=="1" net session >nul 2>&1
if "%NEED_INSTALL%"=="1" if errorlevel 1 (
    echo [ERROR] An install step is required ^(VS Build Tools and/or CMake are missing^),
    echo [ERROR] but this prompt is not elevated. Please re-run this script from an
    echo [ERROR] elevated ^(Run as Administrator^) command prompt.
    exit /b 1
)

rem ── VS Build Tools ────────────────────────────────────────────────────────────
if "%NEED_VS_INSTALL%"=="0" goto vs_skip
echo [INFO] Installing Visual Studio 2022 Build Tools ^(C++ workload + ARM64 tools + Windows SDK^)...
if not exist "%TOOLCHAIN_DIR%" mkdir "%TOOLCHAIN_DIR%"
set "VS_BOOTSTRAPPER=%TOOLCHAIN_DIR%\vs_buildtools.exe"
if exist "%VS_BOOTSTRAPPER%" goto vs_bootstrapper_ready
echo [INFO] Downloading VS Build Tools bootstrapper...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vs_buildtools.exe' -OutFile '%VS_BOOTSTRAPPER%'"
if errorlevel 1 (
    echo [ERROR] Failed to download the VS Build Tools bootstrapper.
    exit /b 1
)
:vs_bootstrapper_ready
echo [INFO] Running the VS Build Tools installer ^(this can take several minutes^)...
"%VS_BOOTSTRAPPER%" --quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --add Microsoft.VisualStudio.Component.Windows10SDK --includeRecommended
set "VS_INSTALL_RESULT=%ERRORLEVEL%"
if "%VS_INSTALL_RESULT%"=="0" goto vs_install_done
if "%VS_INSTALL_RESULT%"=="3010" goto vs_install_done
echo [ERROR] VS Build Tools installation failed with exit code %VS_INSTALL_RESULT%.
exit /b 1
:vs_install_done
echo [INFO] VS Build Tools installed successfully.
goto vs_section_end
:vs_skip
echo [INFO] VS Build Tools with the required workload/components already installed, skipping. ^(%VS_INSTALL_PATH%^)
:vs_section_end

rem ── CMake ─────────────────────────────────────────────────────────────────────
if "%NEED_CMAKE_INSTALL%"=="0" goto cmake_skip
echo [INFO] Installing portable CMake %CMAKE_VERSION% ^(Windows ARM64^)...
if not exist "%TOOLCHAIN_DIR%" mkdir "%TOOLCHAIN_DIR%"
set "CMAKE_ZIP=%TOOLCHAIN_DIR%\cmake.zip"
set "CMAKE_EXTRACT_DIR=%TOOLCHAIN_DIR%\cmake"
if exist "%CMAKE_ZIP%" if exist "%CMAKE_EXTRACT_DIR%" goto cmake_extracted
if exist "%CMAKE_ZIP%" goto cmake_zip_ready
echo [INFO] Downloading CMake %CMAKE_VERSION% portable ZIP...
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/Kitware/CMake/releases/download/v%CMAKE_VERSION%/cmake-%CMAKE_VERSION%-windows-arm64.zip' -OutFile '%CMAKE_ZIP%'"
if errorlevel 1 (
    echo [ERROR] Failed to download the CMake portable ZIP.
    exit /b 1
)
:cmake_zip_ready
echo [INFO] Extracting CMake portable ZIP...
powershell -NoProfile -Command "Expand-Archive -Path '%CMAKE_ZIP%' -DestinationPath '%CMAKE_EXTRACT_DIR%' -Force"
if errorlevel 1 (
    echo [ERROR] Failed to extract the CMake portable ZIP.
    exit /b 1
)
:cmake_extracted
set "CMAKE_BIN_DIR="
for /f "delims=" %%D in ('dir /s /b "%CMAKE_EXTRACT_DIR%\cmake.exe" 2^>nul') do set "CMAKE_BIN_DIR=%%~dpD"
if not defined CMAKE_BIN_DIR (
    echo [ERROR] Could not locate cmake.exe under %CMAKE_EXTRACT_DIR%.
    exit /b 1
)
set "PATH=%CMAKE_BIN_DIR%;%PATH%"
goto cmake_section_end
:cmake_skip
echo [INFO] CMake %CMAKE_CURRENT_VERSION% already available on PATH, skipping install.
:cmake_section_end

cmake --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cmake --version still fails after setup; aborting.
    exit /b 1
)
for /f "tokens=3" %%V in ('cmake --version ^| findstr /b /c:"cmake version"') do set "CMAKE_RESOLVED_VERSION=%%V"
echo [INFO] Using CMake %CMAKE_RESOLVED_VERSION%

rem ── Validate ──────────────────────────────────────────────────────────────────
if not defined QNN_SDK_ROOT (
    echo [ERROR] QNN_SDK_ROOT is not set. Please set it before running this script.
    echo [ERROR] It must point to the Qualcomm AI Runtime ^(QAIRT^) SDK root.
    echo [ERROR] See docs\windows-build-dependencies.md for how to obtain it manually.
    echo [ERROR] Example: set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.x.x.x
    exit /b 1
)
if not exist "%QNN_SDK_ROOT%" (
    echo [ERROR] QNN_SDK_ROOT does not exist: %QNN_SDK_ROOT%
    echo [ERROR] See docs\windows-build-dependencies.md for how to obtain the QAIRT SDK.
    exit /b 1
)

echo ============================================================
echo  GenieAPIService - Windows/ARM64 build
echo ============================================================
echo   QNN_SDK_ROOT     : %QNN_SDK_ROOT%
echo   QNN_STUB_VERSION : %QNN_STUB_VERSION%
echo   BUILD_TYPE       : %BUILD_TYPE%
echo   BACKENDS         : QNN/Genie only
echo   BUILD_AS_DLL     : %BUILD_AS_DLL%
echo   BUILD_DIR        : %BUILD_DIR%
echo ============================================================

rem ── Configure ─────────────────────────────────────────────────────────────────
cmake -S "%SCRIPT_DIR%" -B "%BUILD_DIR%" -A ARM64 -DCMAKE_BUILD_TYPE=%BUILD_TYPE% -DBUILD_AS_DLL=%BUILD_AS_DLL% -DQNN_STUB_VERSION=%QNN_STUB_VERSION%
if errorlevel 1 (
    echo [ERROR] CMake configure step failed.
    exit /b 1
)

rem ── Build ─────────────────────────────────────────────────────────────────────
cmake --build "%BUILD_DIR%" --config %BUILD_TYPE%
if errorlevel 1 (
    echo [ERROR] CMake build step failed.
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete.
echo  Output: %BUILD_DIR%\GenieService-win-arm64\GenieAPIService.exe
echo ============================================================
