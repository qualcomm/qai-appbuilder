# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$process= Get-Process | Where-Object { $_.ProcessName -like "*uv*"}
if($process){
    Stop-Process -Id $process.id -Force
}
$process= Get-Process | Where-Object { $_.ProcessName -like "*langflow*"}
if($process){
    Stop-Process -Id $process.id -Force
}