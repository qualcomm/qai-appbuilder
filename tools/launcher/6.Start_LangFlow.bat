@echo off
set "currentDir=%CD%"

set TOOL_PATH=%currentDir%\tools\pixi;%currentDir%\tools\aria2c;%currentDir%\tools\aria2c\aria2-1.36.0-win-64bit-build1;%currentDir%\tools\wget;%currentDir%\tools\Git\bin;
set PATH=%TOOL_PATH%%PATH%

echo Start langflow...
echo Please keep this window open. It will open the browser with  http://127.0.0.1:8979 automatically ....

powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Start_Langflow.ps1" "%currentDir%"

pause
