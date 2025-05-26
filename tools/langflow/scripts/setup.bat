(
REM #==============================================================================
REM #
REM #  Copyright (c) 2025 Qualcomm Technologies, Inc.
REM #  All Rights Reserved.
REM #  Confidential and Proprietary - Qualcomm Technologies, Inc.
REM #
REM #==============================================================================
)


@echo off
set "user=%USERNAME%"
md "C:\users\%user%\Downloads\qualcomm_temp" 2>nul
pushd "C:\users\%user%\Downloads\qualcomm_temp"




powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://github.qualcomm.com/raw/xiaoshi/langflowcv_package/main/scripts/setup.ps1' -OutFile 'setup.ps1'"
powershell -NoProfile -ExecutionPolicy Bypass .\setup.ps1


popd
echo Now in the folder: %CD%