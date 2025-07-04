# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

# Get current PowerShell version
$version = $PSVersionTable.PSVersion

# Check if the major version is less than 7
if ($version.Major -lt 7) {
    Write-Host "Current PowerShell version $version does not meet the requirement." -ForegroundColor Red
    Write-Host "Please install PowerShell 7 according to the README document and try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
} else {
    Write-Host "PowerShell version meets the requirement: $version" -ForegroundColor Green
    Write-Host "Starting Setup ..." -ForegroundColor Green
}

#Set it to print debug information
Set-PSDebug -Trace 0

#Get the environment 
$userProfilePath = $env:USERPROFILE

# Check if winget is installed
$wingetCommand = Get-Command winget -ErrorAction SilentlyContinue
if (-not $wingetCommand) {
    Write-Host "winget is not installed. Please install winget and try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

# Install git
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    Write-Host "Installing Git..."
    winget install --id Git.Git -e --source winget
}
# Get git command again
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    Write-Host "Git installation failed. Please install Git and try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

# Install Visual C++ Redistributable
# winget install --id=Microsoft.VCRedist.2015+.x64 -e

#Install Pixi
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if (-not $pixiCommand) {
    Write-Host "Installing Pixi..."
    irm -useb https://pixi.sh/install.ps1 | iex
} else {
    Write-Host "Pixi is already installed."
}

# Clone ai-engine-direct-helper repository if not already present
$repoDir = Join-Path $scriptPath "ai-engine-direct-helper"
if (-not (Test-Path $repoDir)) {
    Write-Host "Cloning ai-engine-direct-helper repository..."
    & $gitCommand.Path clone https://github.com/quic/ai-engine-direct-helper.git --depth=1
} else {
    Write-Host "ai-engine-direct-helper repository already exists at $repoDir."
}

Write-Host "Setup Python environment..."
# Change cwd to env
Set-Location $scriptPath\env
# Only for network environment in corporation
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if ($pixiCommand) {
    & $pixiCommand.Path config set tls-no-verify true
    & $pixiCommand.Path install --tls-no-verify --frozen
    & $pixiCommand.Path run install-qai
} else {
    Write-Host "Pixi is not in PATH. Please rerun 1.Setup_QAI_AppBuilder.ps1." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    Set-Location $scriptPath
    exit 1
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
