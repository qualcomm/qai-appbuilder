# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

#Install Pixi
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if (-not $pixiCommand) {
    Write-Host "Installing Pixi..."
    irm -useb https://pixi.sh/install.ps1 | iex
    # winget install prefix-dev.pixi -l tools
} else {
    Write-Host "Pixi is already installed."
}

Write-Host "Setup Python environment..."
Set-Location $scriptPath\env
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if ($pixiCommand) {
    & $pixiCommand.Path config set tls-no-verify true
    & $pixiCommand.Path install --tls-no-verify --frozen
} else {
    Write-Host "Pixi is not in PATH. Please run [1.Setup_QAI_AppBuilder.bat] again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Installation failed. Please try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    Set-Location $scriptPath
    exit 1
}
