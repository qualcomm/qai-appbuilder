@echo off
setlocal enabledelayedexpansion
echo Start Genie Service...

:: Step 1: download GenieAPIService
    if exist "tools" (
        echo tools directory already exists
    ) else (
        echo Creating tools directory...
        mkdir tools
    )

    if exist "tools\GenieAPIService_2.34.zip" (
        echo GenieAPIService_2.34.zip already exists 
    ) else (
        echo Downloading GenieAPIService...
        powershell -Command "Invoke-WebRequest -Uri https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip -OutFile tools\GenieAPIService_2.34.zip"
        if exist "tools\GenieAPIService_2.34.zip" (
            echo download successful 
        ) else (
            echo download failed 
        )
    )
    if exist "ai-engine-direct-helper\samples\GenieAPIService" (
        echo GenieAPIService already exists
    ) else (
        echo Downloading GenieAPIService...
        powershell -Command "Expand-Archive -Path tools\GenieAPIService_2.34.zip -DestinationPath ai-engine-direct-helper\samples"
    )
    echo GenieAPIService downloaded and extracted.

:: Step 2: Start C++ Genie Service
    echo Starting C++ Genie Service...
    cd ai-engine-direct-helper\samples\
    echo Please keep this window open.Genie Service is running
    powershell -Command "GenieAPIService\GenieAPIService.exe -c genie\python\models\IBM-Granite-v3.1-8B\config.json  -l
    echo Genie API Service Started.

echo Start C++ Genie Service Successfully!

