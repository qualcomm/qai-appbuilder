# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

#Check Pixi installation
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if (-not $pixiCommand) {
  Write-Host "Pixi is not installed. Please run 1.Setup_QAI_AppBuilder.ps1 first." -ForegroundColor Red
  Write-Host "Press any key to exit..."
  [void][System.Console]::ReadKey($true)
  exit 1
}

#Get the environment 
$userProfilePath = $env:USERPROFILE

Set-Location $scriptPath\env

Write-Host "Please choose which WebUI to launch:"
Write-Host "1. Start ImageRepairApp"
# Write-Host "2. Start StableDiffusionApp"
Write-Host "2. Start GenieWebUI"
$choice = Read-Host "Enter the number (1-2) corresponding to your choice"

switch ($choice) {
  "1" {
    Write-Host "Launching ImageRepairApp ..."
    pixi run webui-imagerepair
  }
  "2" {
    Write-Host "Launching GenieWebUI ..."
    pixi run webui-genie
  }
  Default {
    Write-Host "Unaccepted option. Please run the script again and choose a valid option."
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    Set-Location $scriptPath
    exit 1
  }
}

Set-Location $scriptPath