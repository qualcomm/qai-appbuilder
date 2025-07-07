@echo off
set "currentDir=%CD%"

echo Start langflow...
echo Please keep this window open. It will open the browser with  http://127.0.0.1:8979 automatically ....

powershell -ExecutionPolicy Bypass -File "%currentDir%\utils\Start_Langflow.ps1" "%currentDir%"

pause
