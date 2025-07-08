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

# Install git
Set-Location $scriptPath
$env:Path += ";tools/Git/bin"
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    Write-Host "Installing Git..."
    $gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.50.0.windows.2/PortableGit-2.50.0.2-arm64.7z.exe"
    $outputPath = "tools/Git-2.50.0.2-arm64.7z.exe"
    Invoke-WebRequest -Uri $gitUrl -OutFile $outputPath

    Set-Location $scriptPath\env
    & $pixiCommand.Path run install-git
    Set-Location $scriptPath
}

# Get git command again
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($gitCommand) {
    $gitPath = Resolve-Path $gitCommand.Source | Select-Object -ExpandProperty Path
} else {
    Write-Host "Git installation failed. Please install Git and try again." -ForegroundColor Red
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

# Clone ai-engine-direct-helper repository if not already present
$repoDir = Join-Path $scriptPath "ai-engine-direct-helper"
if (-not (Test-Path $repoDir)) {
    Write-Host "Cloning ai-engine-direct-helper repository..."
    & $gitPath clone https://github.com/quic/ai-engine-direct-helper.git --depth=1
} else {
    Write-Host "ai-engine-direct-helper repository already exists at $repoDir. Pulling latest changes..."
    Push-Location $repoDir
    & $gitPath pull
    Pop-Location
}

# Install QAI AppBuilder
if ($pixiCommand) {
    Set-Location $scriptPath\env
    & $pixiCommand.Path run install-qai
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
