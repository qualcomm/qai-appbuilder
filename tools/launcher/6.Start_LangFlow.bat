@echo off

echo Start langflow...
echo Please keep this window open. It will open the browser with  http://127.0.0.1:8979 automatically ....
cd env
pixi run "powershell -ExecutionPolicy Bypass -File ../utils/Start_Langflow.ps1"
pause
