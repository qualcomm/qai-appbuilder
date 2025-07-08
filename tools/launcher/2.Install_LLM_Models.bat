@echo off
setlocal enabledelayedexpansion
echo Starting installation...

set PATH=%CD%\ai-engine-direct-helper\samples\tools\aria2c;%CD%\ai-engine-direct-helper\samples\tools\wget;%PATH%

echo Installing large language model...
cd env
pixi run install-model

echo(

if %ERRORLEVEL%==0 (
    echo Install successfully. Press any key to exit...
) else (
    echo Install failed, exit code: %ERRORLEVEL%
)

pause
