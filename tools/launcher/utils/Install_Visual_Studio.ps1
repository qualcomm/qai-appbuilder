# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

Function download_install_VS_Studio {
    $pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
    if ($pixiCommand) {
        Set-Location $scriptPath\env
        & $pixiCommand.Path run install-vs
        Set-Location $scriptPath
    }
    Set-Location $scriptPath
}

$vsWherePath = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vsWherePath) {
    $instances = & $vsWherePath -all -prerelease -property installationPath
    if ($instances) {
        Write-Host "Found Visual Studio:"
        $instances | ForEach-Object { Write-Host "- $_" }
    } else {
        Write-Host "Visual Studio not installed."
        download_install_VS_Studio
    }
} else {
    Write-Host "Visual Studio not installed."
    download_install_VS_Studio
}


