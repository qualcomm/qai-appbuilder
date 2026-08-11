@echo off
REM start.bat — one-click launcher for Track-Anything on Windows on Snapdragon.
REM Creates/uses a local venv, installs deps, then runs the app. Models and the
REM default video are downloaded automatically on first run.
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo Starting Track-Anything...
python track_anything.py

endlocal
pause
