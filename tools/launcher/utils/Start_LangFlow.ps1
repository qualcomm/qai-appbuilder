
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

param (
    [string]$scriptPath
)

$process= Get-Process | Where-Object { $_.ProcessName -like "*langflow*"}
if($process){
    Stop-Process -Id $process.id -Force
}

$process = Get-WmiObject -Class Win32_Process | Where-Object { $_.ProcessName -like "*Genie*.exe" }

if ($process) {
    Write-Host "GenieAPIService is ready now."
} else {
    Write-Host "Can not find GenieAPIService. Please run [4.Start_GenieAPIService.bat] to start GenieAPIService before launch LangFlow!"
    Write-Host "Press any key to exit..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

Start-Job -ScriptBlock {
    $maxAttempts = 60
    $attempt = 0
    $interval = 2  # seconds
    $urlOpened = $false

    while ($attempt -lt $maxAttempts) {
        try {
            $socket = New-Object System.Net.Sockets.TcpClient
            $socket.Connect("127.0.0.1", 8979)
            Start-Sleep -Seconds 1
            try {
                $socket = New-Object System.Net.Sockets.TcpClient
                $socket.Connect("127.0.0.1", 8979)
                if (-not $urlOpened) {
                    Write-Host "LangFlow is ready now. Opening the browser..."
                    Start-Process -FilePath "http://127.0.0.1:8979"
                    $urlOpened = $true
                    return
                }
            }
            catch {
                continue
            }
        }
        catch {
            $attempt++
            Write-Host -ForegroundColor Cyan "Waiting for the service to start... Attempt $attempt of $maxAttempts"
            Start-Sleep -Seconds $interval
        }
    }

    if ($attempt -eq $maxAttempts) {
        Write-Host "Failed to detect the service readiness after $maxAttempts attempts."
    }
}

#Install Pixi
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if (-not $pixiCommand) {
    Write-Host "Installing Pixi..."
    irm -useb https://pixi.sh/install.ps1 | iex
} else {
    Write-Host "Pixi is already installed."
}

Set-Location $scriptPath\env
& $pixiCommand.Path run langflow run --host 0.0.0.0 --port 8979
Set-Location $scriptPath