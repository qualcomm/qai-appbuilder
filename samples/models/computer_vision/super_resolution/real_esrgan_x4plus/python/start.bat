@echo off
REM start.bat — one-click launcher for Real-ESRGAN x4plus on Windows on Snapdragon.
REM Creates/uses a local venv, installs deps, then runs the app. The model and
REM the default input image are downloaded automatically on first run.
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo Starting Real-ESRGAN x4plus...
python real_esrgan_x4plus.py

endlocal
pause
