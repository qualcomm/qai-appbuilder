# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

# Install QAI AppBuilder
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if ($pixiCommand) {
    Set-Location $scriptPath\env
    & $pixiCommand.Path run install-qai-appbuilder
    Set-Location $scriptPath
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Installation failed. Please try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    Set-Location $scriptPath
    exit 1
}

Write-Host "Install successfully. Press any key to exit..." -ForegroundColor Green
Set-Location $scriptPath

[void][System.Console]::ReadKey($true)
