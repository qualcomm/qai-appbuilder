@echo off
setlocal enabledelayedexpansion
echo Starting installation...

set PATH=%CD%\ai-engine-direct-helper\samples\tools\aria2c;%CD%\ai-engine-direct-helper\samples\tools\wget;%PATH%

echo Running Install LLM Models
cd env
pixi run install-model

echo install LLM Models executed successfully
pause
