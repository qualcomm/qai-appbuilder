@echo off
setlocal enabledelayedexpansion
echo Starting installation...

set PATH=%CD%\ai-engine-direct-helper\samples\tools\aria2c;%CD%\ai-engine-direct-helper\samples\tools\wget;%PATH%

echo Installing large language model...
cd env
pixi run install-model

echo Install successfully. Press any key to exit...
pause
