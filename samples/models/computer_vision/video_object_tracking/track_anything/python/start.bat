@echo off
REM ============================================================================
REM start.bat  --  one-click launcher for Track-Anything on Windows on Snapdragon
REM ============================================================================
REM Creates/uses a local venv, installs deps in *ordered independent phases*,
REM then runs the app.  Models and the default video are downloaded automatically
REM on first run.
REM
REM Why the pip installs are split into phases (do not merge them!):
REM
REM   Phase A - core deps (qai_appbuilder / requests / tqdm / numpy)
REM     These are what samples/shared/python/install.py imports at *import time*
REM     to download the model bin and the sample video.  If they are missing the
REM     app crashes with:
REM         ModuleNotFoundError: No module named 'requests'
REM     BEFORE any useful diagnostic can be printed.  So we install them first,
REM     in their own pip transaction, and abort start.bat if that fails.
REM
REM   Phase B - opencv-python-headless (best-effort)
REM     PyPI currently ships NO cp313-win_arm64 wheel for opencv-python or
REM     opencv-python-headless (verified: ``pip download --only-binary=:all:``
REM     returns "no matching distribution" for every 4.x/5.x version).  Pip
REM     therefore falls back to a ~95 MB sdist tarball and tries to build it
REM     from source; the tarball itself cannot be unpacked on Windows because
REM     of a ``video_capture_xaml.WindowsPhone.vcxproj.filters`` path deeper
REM     than MAX_PATH, so pip aborts.  If we had opencv in the same pip
REM     transaction as the core deps, that abort would take requests / tqdm /
REM     qai_appbuilder down with it (exactly the failure you observed).
REM
REM     Mitigations, tried in this order:
REM       1. The venv is created with ``--system-site-packages`` so any
REM          opencv already installed in the parent Python is visible inside
REM          the venv and we skip installing opencv entirely.
REM       2. Otherwise we try ``pip install --only-binary=:all: opencv-python-headless``
REM          in case a wheel appears in the future.
REM       3. If that still fails we print a clear "please install opencv
REM          manually into your parent Python" message and stop, instead of
REM          launching the app just to have it die on ``import cv2``.
REM
REM   Phase C - remaining app deps (onnxruntime, matplotlib)
REM     Same reasoning: install last, in their own transaction, so a single
REM     download failure never poisons the earlier phases.
REM ============================================================================
setlocal EnableDelayedExpansion
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

cd /d "%~dp0"

REM --- Create the venv with system-site-packages so a system-wide opencv is
REM     reused instead of pip trying (and failing) to build one from source.
if not exist ".venv" (
    echo Creating virtual environment ^(--system-site-packages, needed for opencv on Win-ARM^)...
    python -m venv --system-site-packages .venv || goto :venv_error
)

call .venv\Scripts\activate.bat

echo.
echo [1/4] Upgrading pip...
python -m pip install --upgrade pip || goto :pip_error

echo.
echo [2/4] Installing CORE deps ^(qai_appbuilder / requests / tqdm / numpy^)...
REM --prefer-binary avoids pip trying to build packages from a source tarball
REM when no matching win_arm64 wheel is published for a version.
pip install --prefer-binary "qai_appbuilder>=2.24.0" requests tqdm numpy || goto :pip_error

echo.
echo [3/4] Ensuring OpenCV is available ^(best-effort^)...
REM First check if cv2 is already importable (system-site-packages, or a
REM previous run).  If so, skip pip entirely — this is the fast path on
REM Windows-on-ARM where no wheel is published on PyPI.
python -c "import cv2, sys; print('    -> Using existing OpenCV', cv2.__version__)" 2>nul
if !ERRORLEVEL! EQU 0 (
    echo     ^(cv2 already installed; no pip install needed^)
) else (
    echo     cv2 not found in the venv; attempting ``pip install --only-binary=:all: opencv-python-headless``...
    pip install --prefer-binary --only-binary=:all: opencv-python-headless 2>nul
    if !ERRORLEVEL! NEQ 0 (
        echo     [WARN] No opencv-python-headless wheel is available for this Python.
        echo     [WARN] Trying opencv-python ^(non-headless, may also be sdist-only^)...
        pip install --prefer-binary --only-binary=:all: opencv-python 2>nul
    )
    python -c "import cv2" 2>nul
    if !ERRORLEVEL! NEQ 0 goto :opencv_error
)

echo.
echo [4/4] Installing app deps ^(onnxruntime, matplotlib^)...
pip install --prefer-binary onnxruntime matplotlib || goto :pip_error

echo.
echo Starting Track-Anything...
python track_anything.py
goto :end

REM ---------------------------------------------------------------------------
REM Error handlers
REM ---------------------------------------------------------------------------
:venv_error
echo.
echo [ERROR] Failed to create the virtual environment at .venv .
echo         Make sure Python 3.11+ is on PATH and try again.
exit /b 1

:pip_error
echo.
echo [ERROR] Dependency installation failed - see the pip output above.
echo         Track-Anything will NOT be started because required packages are
echo         missing.  Fix the pip error ^(usually a proxy/network issue^) and
echo         run start.bat again.
exit /b 1

:opencv_error
echo.
echo ============================================================================
echo [ERROR] OpenCV is required but could not be installed automatically.
echo.
echo         PyPI currently ships no cp313-win_arm64 wheel for
echo         opencv-python-headless or opencv-python, and building from the
echo         sdist fails on Windows because of a MAX_PATH violation inside the
echo         source tarball.
echo.
echo         Please install OpenCV into your *parent* Python once:
echo             python -m pip install opencv-python-headless
echo         then delete this venv ^(``rmdir /S /Q .venv``^) and re-run start.bat.
echo         The venv is created with --system-site-packages, so a system-wide
echo         install will be picked up automatically.
echo ============================================================================
exit /b 1

:end
endlocal
pause


