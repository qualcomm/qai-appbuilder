@echo off
setlocal enabledelayedexpansion
echo Start Genie Service...

:: Start C++ Genie Service
    echo Starting C++ Genie Service...
    cd ai-engine-direct-helper\samples\
    echo Please keep this window open. Genie Service is running
    powershell -Command "GenieAPIService\GenieAPIService.exe -c genie\python\models\IBM-Granite-v3.1-8B\config.json  -l
    echo Genie API Service Started.

echo Start C++ Genie Service Successfully!
