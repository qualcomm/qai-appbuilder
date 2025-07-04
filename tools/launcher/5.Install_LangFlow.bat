@echo off
setlocal enabledelayedexpansion
echo Start install langflow...

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
