@echo off
:: Run as administrator
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This script requires administrator privileges. Please right-click and run as administrator.
    pause
    exit /b
)

:: Step 1: Install Python dependencies for WebUI
echo Installing Python packages for WebUI...
pip install gradio qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers

:: Step 2: Change to samples directory
cd ai-engine-direct-helper\samples

:MENU
cls
echo ============================================
echo QAI AppBuilder WebUI Launcher
echo ============================================
echo 1. Start ImageRepairApp
echo 2. Start StableDiffusionApp
echo 3. Start GenieWebUI
echo 4. Exit
echo ============================================
set /p choice=Enter your choice [1-4]:

if "%choice%"=="1" (
    python webui\ImageRepairApp.py
    goto MENU
)
if "%choice%"=="2" (
    python webui\StableDiffusionApp.py
    goto MENU
)
if "%choice%"=="3" (
    python webui\GenieWebUI.py
    goto MENU
)
if "%choice%"=="4" (
    exit
)
goto MENU
