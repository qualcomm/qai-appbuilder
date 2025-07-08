@echo off
setlocal enabledelayedexpansion
echo Start install langflow...

if exist "tools" (
    echo tools directory already exists
) else (
    echo Creating tools directory...
    mkdir tools
)

:Install Visual Studio
powershell -ExecutionPolicy Bypass -File utils\Install_Visual_Studio.ps1"

cd env
pixi update
pixi run "langflow -v"

if %ERRORLEVEL% equ 0 (
    echo [%date% %time%] Langflow has been installed
    echo Langflow has been installed
) else (
    echo [%date% %time%] Langflow is not installed
    pixi install --tls-no-verify --frozen
    pixi run install-langflow
)
pause
