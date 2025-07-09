@echo off
setlocal enabledelayedexpansion
echo Starting installation...

set "currentDir=%CD%"

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\aria2c\aria2-1.36.0-win-64bit-build1;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

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
