@echo off
REM =====================================================================
REM Setup_Env.bat (Simplified - x64 Python 3.10 Only)
REM AIPC Environment Setup
REM =====================================================================
REM
REM Installs all dependencies required by aipc:
REM   1. aria2c (multi-thread downloader - downloaded first via PowerShell)
REM   2. uv (Python package manager, x64 - downloaded via aria2c)
REM   3. x86_64 Python 3.10 venv (for QAIRT model conversion)
REM   4. QAIRT SDK 2.45.40.260406 (downloads ~2GB via aria2c)
REM   5. Build tools (VS 2022) via scripts\install_vs.ps1
REM   6. QAIRT dependency verification
REM   7. Generate config/qairt_env.json
REM
REM Usage:
REM   Setup_Env.bat
REM   Setup_Env.bat --no-pause
REM   Setup_Env.bat --sdk-root "D:\QAIRT\2.45.40.260406"
REM
REM Runtime (after setup, in PowerShell):
REM   . "$env:QAIRT_SDK_ROOT\bin\envsetup.ps1"
REM =====================================================================

setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "_T=%TIME: =0%"
for /f "tokens=1-3 delims=:." %%a in ("%_T%") do set /a "_START_S=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)"

set "UV_BIN_DIR=bin\uv"
set "UV_EXE=bin\uv\uv.exe"
set "UV_URL=https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
set "UV_ZIP=downloads\_uv_tmp.zip"
set "ARIA2C_EXE=bin\aria2c\aria2c.exe"
set "ARIA2C_URL=https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
set "ARIA2C_ZIP=downloads\_aria2c_tmp.zip"
set "VENV_310_DIR=venv\.venv_x64_310"
set "CONFIG_DIR=config"
set "VENDOR_QAIRT_DIR=vendor\qairt"
set "SETUP_HELPER=scripts\setup_qairt_env.py"

REM -- QAIRT SDK settings (configurable via environment variables) --------------
if not defined QAIRT_VERSION set "QAIRT_VERSION=2.45.40.260406"
if not defined QAIRT_SDK_ROOT set "QAIRT_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\%QAIRT_VERSION%"
if not defined QAIRT_DOWNLOAD_URL set "QAIRT_DOWNLOAD_URL=https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/%QAIRT_VERSION%/v%QAIRT_VERSION%.zip"
set "QAIRT_URL=%QAIRT_DOWNLOAD_URL%"
set "QAIRT_ZIP=downloads\_qairt_tmp.zip"
set "QAIRT_VENDOR_ZIP=%VENDOR_QAIRT_DIR%\v%QAIRT_VERSION%.zip"

REM Parse arguments
set "NO_PAUSE=0"
:parse_args
if "%~1"=="" goto :start_setup
if /i "%~1"=="--no-pause" ( set "NO_PAUSE=1" & shift & goto :parse_args )
if /i "%~1"=="--sdk-root" ( set "QAIRT_SDK_ROOT=%~2" & shift & shift & goto :parse_args )
shift & goto :parse_args

:start_setup
echo.
echo  +----------------------------------------------------------+
echo  ^|   AIPC  -  Setup Environment                            ^|
echo  ^|   (x64 Python 3.10 Only)                                ^|
echo  +----------------------------------------------------------+
echo.

if not exist "bin\uv" mkdir "bin\uv"
if not exist "bin\aria2c" mkdir "bin\aria2c"
if not exist "venv" mkdir "venv"
if not exist "downloads" mkdir "downloads"

echo.
echo -- Step 1: aria2c (multi-thread downloader) ---------------------
call :install_aria2c
if errorlevel 1 goto :error_exit

echo.
echo -- Step 2: uv (Python package manager, x64) --------------------
call :install_uv
if errorlevel 1 goto :error_exit

echo.
echo -- Step 3: x86_64 Python 3.10 venv (for model conversion) ------
call :install_venv_310
if errorlevel 1 goto :error_exit

echo.
echo -- Step 4: QAIRT SDK %QAIRT_VERSION% --------------------------
call :install_qairt_sdk
if errorlevel 1 goto :error_exit

echo.
echo -- Step 5: Install / update VS 2022 build tools (fully silent) --
if exist "scripts\install_vs.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\install_vs.ps1" -LogDir "downloads"
    if errorlevel 1 echo [WARN] VS installation step encountered an issue.
) else (
    echo [SKIP] scripts\install_vs.ps1 not found, skipping VS build tools install.
)

echo.
echo -- Step 5b: QAIRT dependency verification (informational) -------
set "QAIRT_CHECK_DEP=%QAIRT_SDK_ROOT%\bin\check-windows-dependency.ps1"
if not exist "%QAIRT_CHECK_DEP%" (
    echo [WARN] check-windows-dependency.ps1 not found at: %QAIRT_CHECK_DEP%
) else (
    echo [INFO] Running QAIRT dependency checker ^(dry-run, informational only^)...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; & '%QAIRT_CHECK_DEP%' -DryRun" 2>nul
    echo [INFO] QAIRT dependency check complete.
)

echo.
echo -- Step 6: Generate SKILL environment config --------------------
call :gen_config
if errorlevel 1 goto :error_exit

echo.
echo -- Step 7: Verify installation ----------------------------------
call :verify_install

echo.
echo  +----------------------------------------------------------+
echo  ^|   [OK] Setup complete!                                  ^|
echo  ^|                                                          ^|
echo  ^|   AIPC environment is ready to use.                     ^|
echo  +----------------------------------------------------------+
echo.
echo [INFO] To activate the QAIRT SDK environment (run in PowerShell):
echo [INFO]   . "%QAIRT_SDK_ROOT%\bin\envsetup.ps1"
echo.
echo [INFO] To activate x64 Python 3.10 venv:
echo [INFO]   PythonShell.bat
echo.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 0


REM ===================================================================
REM  Subroutines
REM ===================================================================

:install_aria2c
REM Check if aria2c already exists
if exist "%ARIA2C_EXE%" (
    echo [SKIP] aria2c already installed: %ARIA2C_EXE%
    exit /b 0
)

echo [INFO] Downloading aria2c (x64)...
echo [INFO] URL: %ARIA2C_URL%
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%ARIA2C_URL%' -OutFile '%ARIA2C_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo [WARN] Failed to download aria2c. Will use PowerShell for all downloads.
    del /f /q "%ARIA2C_ZIP%" 2>nul
    exit /b 0
)

echo [INFO] Extracting aria2c.exe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '%ARIA2C_ZIP%' -DestinationPath 'downloads\_aria2c_tmp' -Force; " ^
    "Get-ChildItem 'downloads\_aria2c_tmp' -Recurse -Filter 'aria2c.exe' | Copy-Item -Destination '%ARIA2C_EXE%' -Force; " ^
    "Remove-Item 'downloads\_aria2c_tmp' -Recurse -Force"
del /f /q "%ARIA2C_ZIP%" 2>nul

if exist "%ARIA2C_EXE%" (
    echo [OK]   aria2c installed: %ARIA2C_EXE%
) else (
    echo [WARN] aria2c extraction failed. Will use PowerShell for all downloads.
)
exit /b 0


:install_uv
REM Check if uv already exists
if exist "%UV_EXE%" (
    echo [SKIP] uv already installed: %UV_EXE%
    goto :uv_ready
)

echo [INFO] Downloading uv (x64)...
echo [INFO] URL: %UV_URL%

REM Use aria2c if available, otherwise PowerShell
if not exist "%ARIA2C_EXE%" goto :uv_download_powershell

echo [INFO] Using aria2c (16 threads, resume enabled, with progress display)...
if not exist "downloads\log" mkdir "downloads\log"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$proc = Start-Process -FilePath '%ARIA2C_EXE%' " ^
    "  -ArgumentList '-x16 -s16 -k5M -c --file-allocation=none --enable-rpc --rpc-listen-port=6801 --rpc-allow-origin-all -d downloads -o _uv_tmp.zip \"%UV_URL%\"' " ^
    "  -NoNewWindow -PassThru " ^
    "  -RedirectStandardOutput 'downloads\log\aria2c_uv.log' " ^
    "  -RedirectStandardError  'downloads\log\aria2c_uv.err.log'; " ^
    "Start-Sleep -Seconds 2; " ^
    "while ($true) { " ^
    "  if ($proc.HasExited) { break }; " ^
    "  try { " ^
    "    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:6801/jsonrpc' -Method Post " ^
    "      -Body '{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"aria2.tellActive\",\"params\":[[]]}' " ^
    "      -ContentType 'application/json' -TimeoutSec 3 -ErrorAction Stop; " ^
    "    $t = $r.result | Select-Object -First 1; " ^
    "    if ($t -and [long]$t.totalLength -gt 0) { " ^
    "      $pct    = [math]::Round([long]$t.completedLength / [long]$t.totalLength * 100, 1); " ^
    "      $dlMB   = [math]::Round([long]$t.completedLength / 1MB, 0); " ^
    "      $totMB  = [math]::Round([long]$t.totalLength / 1MB, 0); " ^
    "      $spd    = [math]::Round([long]$t.downloadSpeed / 1MB, 1); " ^
    "      $eta    = if ([long]$t.downloadSpeed -gt 0) { [int](([long]$t.totalLength - [long]$t.completedLength) / [long]$t.downloadSpeed) } else { 0 }; " ^
    "      $etaStr = '{0}m{1:D2}s' -f [math]::Floor($eta/60), ($eta %% 60); " ^
    "      $filled = [int]($pct / 5); " ^
    "      $bar    = '[' + ('=' * $filled).PadRight(20) + ']'; " ^
    "      $line   = '[DL] ' + $bar + ' ' + $pct + '%% ' + $dlMB + '/' + $totMB + ' MB  ' + $spd + ' MB/s  ETA ' + $etaStr; " ^
    "      [Console]::Write($line.PadRight(78) + \"`r\") " ^
    "    } " ^
    "  } catch { }; " ^
    "  if ($r -and $r.result.Count -eq 0) { " ^
    "    Start-Sleep -Milliseconds 500; " ^
    "    if ((Test-Path 'downloads\_uv_tmp.zip') -and -not (Test-Path 'downloads\_uv_tmp.zip.aria2')) { break } " ^
    "  }; " ^
    "  Start-Sleep -Seconds 2 " ^
    "}; " ^
    "[Console]::WriteLine(''); " ^
    "if ($proc -and -not $proc.HasExited) { $proc | Stop-Process -Force -ErrorAction SilentlyContinue }; " ^
    "Write-Host '[INFO] aria2c download finished.'"
if exist "downloads\log" rmdir /s /q "downloads\log" 2>nul
if exist "%UV_ZIP%" goto :uv_extract
echo [WARN] aria2c download failed for uv, falling back to PowerShell...

:uv_download_powershell
echo [INFO] Using PowerShell to download uv...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%UV_URL%' -OutFile '%UV_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo [ERROR] Failed to download uv. Check network or proxy settings.
    exit /b 1
)

:uv_extract
if not exist "%UV_ZIP%" (
    echo [ERROR] uv zip not found after download: %UV_ZIP%
    exit /b 1
)

echo [INFO] Extracting uv.exe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '%UV_ZIP%' -DestinationPath '%UV_BIN_DIR%' -Force"
if errorlevel 1 (
    del /f /q "%UV_ZIP%" 2>nul
    echo [ERROR] Failed to extract uv.
    exit /b 1
)
del /f /q "%UV_ZIP%" 2>nul

if exist "%UV_EXE%" (
    echo [OK]   uv installed: %UV_EXE%
) else (
    echo [ERROR] uv.exe not found after extraction: %UV_EXE%
    exit /b 1
)

:uv_ready
set "PATH=%UV_BIN_DIR%;%PATH%"
REM Use Windows native TLS so uv trusts the system certificate store (corporate proxy support)
set "UV_SYSTEM_CERTS=1"
exit /b 0


:install_venv_310
REM Check if venv_310 already exists
if exist "%VENV_310_DIR%\Scripts\python.exe" (
    echo [SKIP] venv_310 already exists: %VENV_310_DIR%
    goto :venv_310_install_deps
)

echo [INFO] Installing x86_64 Python 3.10 via uv...
uv python install cpython-3.10-windows-x86_64
if errorlevel 1 (
    echo [ERROR] Failed to install x86_64 Python 3.10
    exit /b 1
)

echo [INFO] Creating venv_310 (x86_64 Python 3.10)...
uv venv "%VENV_310_DIR%" --python cpython-3.10-windows-x86_64 --seed
if errorlevel 1 (
    echo [ERROR] Failed to create venv_310
    exit /b 1
)
echo [OK]   venv_310 created: %VENV_310_DIR%

:venv_310_install_deps
echo [INFO] Installing QAIRT converter dependencies into venv_310...
if not exist "%VENV_310_DIR%\Scripts\python.exe" (
    echo [WARN] venv_310 Python not found, skipping dep install: %VENV_310_DIR%\Scripts\python.exe
    goto :venv_310_ready
)
if not exist "%SETUP_HELPER%" (
    echo [SKIP] Setup helper not found: %SETUP_HELPER%
    goto :venv_310_ready
)
"%VENV_310_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --install-python-deps
if errorlevel 1 (
    echo [WARN] Some Python deps failed to install in venv_310
)
:venv_310_ready
echo [OK]   venv_310 ready
exit /b 0


:install_qairt_sdk
REM Check if QAIRT SDK already installed
if exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [SKIP] QAIRT SDK already installed: %QAIRT_SDK_ROOT%
    exit /b 0
)

REM Check for pre-placed vendor zip (offline install)
if exist "%QAIRT_VENDOR_ZIP%" (
    echo [INFO] Found vendor QAIRT zip: %QAIRT_VENDOR_ZIP%
    goto :extract_qairt_sdk
)

REM Download via aria2c or PowerShell
echo [INFO] Downloading QAIRT SDK %QAIRT_VERSION% (~2GB)...
echo [INFO] URL: %QAIRT_URL%
echo [INFO] This may take several minutes depending on network speed.
echo [INFO] Download supports resume (safe to interrupt and re-run).
echo.

REM Check if aria2c exists and download
if exist "%ARIA2C_EXE%" goto :download_with_aria2c
goto :download_with_powershell

:download_with_aria2c
echo [INFO] Using aria2c (16 threads, resume enabled, with progress display)...
if not exist "downloads\log" mkdir "downloads\log"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$proc = Start-Process -FilePath '%ARIA2C_EXE%' " ^
    "  -ArgumentList '-x16 -s16 -k10M -c --file-allocation=none --enable-rpc --rpc-listen-port=6800 --rpc-allow-origin-all -d downloads -o _qairt_tmp.zip \"%QAIRT_URL%\"' " ^
    "  -NoNewWindow -PassThru " ^
    "  -RedirectStandardOutput 'downloads\log\aria2c.log' " ^
    "  -RedirectStandardError  'downloads\log\aria2c.err.log'; " ^
    "Start-Sleep -Seconds 2; " ^
    "while ($true) { " ^
    "  if ($proc.HasExited) { break }; " ^
    "  try { " ^
    "    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:6800/jsonrpc' -Method Post " ^
    "      -Body '{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"aria2.tellActive\",\"params\":[[]]}' " ^
    "      -ContentType 'application/json' -TimeoutSec 3 -ErrorAction Stop; " ^
    "    $t = $r.result | Select-Object -First 1; " ^
    "    if ($t -and [long]$t.totalLength -gt 0) { " ^
    "      $pct    = [math]::Round([long]$t.completedLength / [long]$t.totalLength * 100, 1); " ^
    "      $dlMB   = [math]::Round([long]$t.completedLength / 1MB, 0); " ^
    "      $totMB  = [math]::Round([long]$t.totalLength / 1MB, 0); " ^
    "      $spd    = [math]::Round([long]$t.downloadSpeed / 1MB, 1); " ^
    "      $eta    = if ([long]$t.downloadSpeed -gt 0) { [int](([long]$t.totalLength - [long]$t.completedLength) / [long]$t.downloadSpeed) } else { 0 }; " ^
    "      $etaStr = '{0}m{1:D2}s' -f [math]::Floor($eta/60), ($eta %% 60); " ^
    "      $filled = [int]($pct / 5); " ^
    "      $bar    = '[' + ('=' * $filled).PadRight(20) + ']'; " ^
    "      $line   = '[DL] ' + $bar + ' ' + $pct + '%% ' + $dlMB + '/' + $totMB + ' MB  ' + $spd + ' MB/s  ETA ' + $etaStr; " ^
    "      [Console]::Write($line.PadRight(78) + \"`r\") " ^
    "    } " ^
    "  } catch { }; " ^
    "  if ($r -and $r.result.Count -eq 0) { " ^
    "    Start-Sleep -Milliseconds 500; " ^
    "    if ((Test-Path 'downloads\_qairt_tmp.zip') -and -not (Test-Path 'downloads\_qairt_tmp.zip.aria2')) { break } " ^
    "  }; " ^
    "  Start-Sleep -Seconds 2 " ^
    "}; " ^
    "[Console]::WriteLine(''); " ^
    "if ($proc -and -not $proc.HasExited) { $proc | Stop-Process -Force -ErrorAction SilentlyContinue }; " ^
    "Write-Host '[INFO] aria2c download finished.'"
goto :check_download_result

:download_with_powershell
echo [INFO] Using PowerShell (single thread)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%QAIRT_URL%' -OutFile '%QAIRT_ZIP%' -UseBasicParsing"

:check_download_result
REM Clean up aria2c log files (no longer needed after download)
if exist "downloads\log" rmdir /s /q "downloads\log" 2>nul

if not exist "%QAIRT_ZIP%" (
    echo [ERROR] QAIRT SDK download failed.
    echo [INFO]  You can manually download from:
    echo [INFO]  %QAIRT_URL%
    echo [INFO]  Place the zip at: %QAIRT_VENDOR_ZIP%
    echo [INFO]  Then re-run this script.
    exit /b 1
)
set "QAIRT_VENDOR_ZIP=%QAIRT_ZIP%"

:extract_qairt_sdk
echo [INFO] Extracting QAIRT SDK to %QAIRT_SDK_ROOT%...
echo [INFO] This may take a few minutes...

set "QAIRT_EXTRACT_TMP=C:\Qualcomm\AIStack\QAIRT\_extract_tmp"
if not exist "C:\Qualcomm\AIStack\QAIRT" mkdir "C:\Qualcomm\AIStack\QAIRT"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; " ^
    "Write-Host '[INFO] Extracting...'; " ^
    "Expand-Archive -Path '%QAIRT_VENDOR_ZIP%' -DestinationPath '%QAIRT_EXTRACT_TMP%' -Force; " ^
    "$src = Get-ChildItem '%QAIRT_EXTRACT_TMP%' -Recurse -Directory | Where-Object { $_.Name -eq '%QAIRT_VERSION%' } | Select-Object -First 1; " ^
    "if ($src) { if (Test-Path '%QAIRT_SDK_ROOT%') { Remove-Item '%QAIRT_SDK_ROOT%' -Recurse -Force }; Move-Item $src.FullName '%QAIRT_SDK_ROOT%' -Force; Write-Host '[INFO] Moved to %QAIRT_SDK_ROOT%' } " ^
    "else { Write-Host '[WARN] Version subfolder not found, leaving extracted content in %QAIRT_EXTRACT_TMP%' }; " ^
    "if (Test-Path '%QAIRT_EXTRACT_TMP%') { Remove-Item '%QAIRT_EXTRACT_TMP%' -Recurse -Force }; " ^
    "Write-Host '[INFO] Extraction complete.'"
if errorlevel 1 (
    echo [ERROR] Failed to extract QAIRT SDK
    exit /b 1
)

REM Clean up downloaded zip (keep vendor zip if it was pre-placed)
if "%QAIRT_VENDOR_ZIP%"=="%QAIRT_ZIP%" (
    del /f /q "%QAIRT_ZIP%" 2>nul
)

if exist "%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe" (
    echo [OK]   QAIRT SDK installed: %QAIRT_SDK_ROOT%
) else (
    echo [WARN] QAIRT SDK extracted but expected tools not found at %QAIRT_SDK_ROOT%
    echo [INFO] The zip may have extracted to a different subdirectory.
    echo [INFO] Check: C:\Qualcomm\AIStack\QAIRT\
)
exit /b 0


:gen_config
echo [INFO] Generating config\qairt_env.json...
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%SETUP_HELPER%" (
    echo [SKIP] Setup helper not found: %SETUP_HELPER%
    exit /b 0
)
"%VENV_310_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --gen-config --sdk-root "%QAIRT_SDK_ROOT%"
if errorlevel 1 (
    echo [ERROR] Failed to generate config\qairt_env.json
    exit /b 1
)
echo [OK]   config\qairt_env.json generated
exit /b 0


:verify_install
echo [INFO] Running verification...
if not exist "%SETUP_HELPER%" (
    echo [SKIP] Setup helper not found: %SETUP_HELPER%
    exit /b 0
)
"%VENV_310_DIR%\Scripts\python.exe" "%SETUP_HELPER%" --verify --sdk-root "%QAIRT_SDK_ROOT%"
REM Verification failures are warnings, not fatal errors
exit /b 0


:error_exit
echo.
echo [ERROR] Setup failed. See errors above.
call :print_elapsed
if "%NO_PAUSE%"=="0" pause
exit /b 1


:print_elapsed
set "_T=%TIME: =0%"
for /f "tokens=1-3 delims=:." %%a in ("%_T%") do set /a "_END_S=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)"
set /a "_ELAPSED=_END_S-_START_S"
if %_ELAPSED% lss 0 set /a "_ELAPSED+=86400"
set /a "_EM=_ELAPSED/60"
set /a "_ES=_ELAPSED%%60"
echo [INFO] Total elapsed: %_EM%m %_ES%s
exit /b 0
