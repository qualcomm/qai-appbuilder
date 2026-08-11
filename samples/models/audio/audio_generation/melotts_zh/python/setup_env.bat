@echo off
REM ---------------------------------------------------------------------
REM Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
REM SPDX-License-Identifier: BSD-3-Clause
REM ---------------------------------------------------------------------
REM
REM One-time environment setup for the MeloTTS-ZH sample.
REM Supports both ARM64 (Windows on Snapdragon) and x86/AMD64 Python.
REM
REM Reproduces the verified install procedure documented in README.md and
REM NOTES.md:
REM   1. core runtime deps (qai_appbuilder, numpy, soundfile, ...)  [WITH deps]
REM   2. torch CPU wheel (ARM64: special index; x86/AMD64: default PyPI)
REM   3. melotts  from a PATCHED sdist (PyPI sdist is broken)       [NOTES Step 4]
REM   4. the rest of the deps from requirements.txt                 [--no-deps]
REM   5. patch melo/text/japanese.py (three edits)                 [NOTES Step 6]
REM
REM Python auto-detection order:
REM   - ARM64: Python313-arm64, Python312-arm64, Python311-arm64
REM   - x86/AMD64: Python313, Python312, Python311 (both LOCALAPPDATA and C:\Python*)
REM   - Fallback: whatever 'python' resolves to on PATH
REM
REM Each interpreter has its OWN site-packages. If you switch Python
REM (e.g. x64 -> ARM64), you must re-run this script for the new interpreter,
REM or you will hit "ModuleNotFoundError: No module named 'melo'".
REM
REM Run ONCE from this folder:
REM     setup_env.bat
REM
REM To force a specific interpreter, set PYTHON_EXE before calling:
REM     set PYTHON_EXE=C:\Python312\python.exe && setup_env.bat
REM ---------------------------------------------------------------------

setlocal
set "SCRIPT_DIR=%~dp0"

REM ── Python auto-detection ──────────────────────────────────────────────────
REM If PYTHON_EXE is already set (e.g. by the caller), skip auto-detection.
if defined PYTHON_EXE goto :python_found

REM 1. Prefer a native ARM64 Python on WoS (Windows on Snapdragon).
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313-arm64\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312-arm64\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311-arm64\python.exe"
    "C:\Python313-arm64\python.exe"
    "C:\Python312-arm64\python.exe"
    "C:\Python311-arm64\python.exe"
) do (
    if not defined PYTHON_EXE if exist "%%~P" set "PYTHON_EXE=%%~P"
)

REM 2. Fall back to x86/AMD64 Python (standard install locations).
if not defined PYTHON_EXE (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Program Files\Python313\python.exe"
        "C:\Program Files\Python312\python.exe"
        "C:\Program Files\Python311\python.exe"
    ) do (
        if not defined PYTHON_EXE if exist "%%~P" set "PYTHON_EXE=%%~P"
    )
)

REM 3. Last resort: whatever 'python' resolves to on PATH.
if not defined PYTHON_EXE set "PYTHON_EXE=python"

:python_found
echo [INFO] Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version
if errorlevel 1 (
    echo [ERROR] Python was not found. Please install Python 3.10-3.13 first.
    exit /b 1
)

REM ── Detect CPU architecture of the chosen interpreter ─────────────────────
REM Used to decide which PyTorch index to use (ARM64 needs the CPU-only index;
REM x86/AMD64 can use the default PyPI index which has win_amd64 wheels).
"%PYTHON_EXE%" -c "import platform; print(platform.machine().upper())" > "%TEMP%\py_arch.txt" 2>&1
set /p PY_ARCH=<"%TEMP%\py_arch.txt"
del "%TEMP%\py_arch.txt" 2>NUL
echo [INFO] Python architecture: %PY_ARCH%

echo [INFO] Upgrading pip ...
"%PYTHON_EXE%" -m pip install --upgrade pip

REM --- 1. Core runtime (WITH deps) -------------------------------------------
echo [INFO] Installing core runtime deps ...
"%PYTHON_EXE%" -m pip install qai_appbuilder numpy soundfile requests pyyaml
if errorlevel 1 goto :err

REM --- 2. PyTorch CPU wheel (no win_arm64 wheel on the default PyPI index) ----
echo [INFO] Installing torch (CPU wheel) ...
"%PYTHON_EXE%" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto :err

REM --- 3. melotts from a PATCHED sdist (NOTES.md Step 4) ----------------------
REM The PyPI sdist ships a setup.py that reads a requirements.txt it does not
REM include, so `pip install melotts` fails. Download the tarball, inject an
REM empty requirements.txt, install locally without build isolation.
set "MELO_VER=0.1.1"
set "CACHE=%SCRIPT_DIR%..\models\pip_cache"
set "MELO_SRC=%CACHE%\melotts-%MELO_VER%"

"%PYTHON_EXE%" -c "import melo" 2>NUL
if not errorlevel 1 (
    echo [SKIP] melotts already installed.
    goto :melo_done
)

if not exist "%CACHE%" mkdir "%CACHE%"
echo [INFO] Downloading melotts %MELO_VER% sdist ...
REM Use curl (built-in on Windows 10+) to download the sdist tarball.
curl -k -L --progress-bar -o "%CACHE%\melotts-%MELO_VER%.tar.gz" ^
    "https://files.pythonhosted.org/packages/source/m/melotts/melotts-%MELO_VER%.tar.gz"
if errorlevel 1 goto :err

echo [INFO] Extracting and patching sdist (inject empty requirements.txt) ...
tar -xzf "%CACHE%\melotts-%MELO_VER%.tar.gz" -C "%CACHE%"
if errorlevel 1 goto :err
type nul > "%MELO_SRC%\requirements.txt"

echo [INFO] Installing melotts (--no-deps --no-build-isolation) ...
"%PYTHON_EXE%" -m pip install --no-deps --no-build-isolation "%MELO_SRC%"
if errorlevel 1 goto :err
:melo_done

REM --- 4. Remaining deps from requirements.txt (--no-deps) --------------------
echo [INFO] Installing requirements.txt (--no-deps) ...
"%PYTHON_EXE%" -m pip install --no-deps -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 goto :err

REM --- 5. Patch melo/text/japanese.py (NOTES.md Step 6) ----------------------
echo [INFO] Patching melo/text/japanese.py ...
"%PYTHON_EXE%" "%SCRIPT_DIR%patch_melo_japanese.py"
if errorlevel 1 goto :err

echo.
echo [INFO] Environment setup complete.
echo [INFO] Run:  python melotts_zh.py --text "你好世界。"
echo [INFO] (models auto-download on first run; first run also fetches the
echo [INFO]  ~1.3 GB hfl/chinese-roberta-wwm-ext-large BERT model from HuggingFace)

REM --- Cleanup: remove temporary pip_cache / models directory -----------------
if exist "%SCRIPT_DIR%..\models" (
    echo [INFO] Cleaning up temporary directory: %SCRIPT_DIR%..\models
    rmdir /s /q "%SCRIPT_DIR%..\models"
)
endlocal
exit /b 0

:err
echo.
echo [ERROR] Setup failed. See the message above and README.md / NOTES.md.
REM --- Cleanup: remove temporary pip_cache / models directory -----------------
if exist "%SCRIPT_DIR%..\models" (
    echo [INFO] Cleaning up temporary directory: %SCRIPT_DIR%..\models
    rmdir /s /q "%SCRIPT_DIR%..\models"
)
endlocal
exit /b 1
