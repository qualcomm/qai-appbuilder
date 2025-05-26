#==============================================================================
#
#  Copyright (c) 2025 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
#==============================================================================


$process= Get-Process | Where-Object { $_.ProcessName -like "*uv*"}
if($process){
    Stop-Process -Id $process.id -Force
}
$process= Get-Process | Where-Object { $_.ProcessName -like "*langflow*"}
if($process){
    Stop-Process -Id $process.id -Force
}