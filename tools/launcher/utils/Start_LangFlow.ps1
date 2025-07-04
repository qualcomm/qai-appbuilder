
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$process= Get-Process | Where-Object { $_.ProcessName -like "*langflow*"}
if($process){
    #Write-Output "Found langflow process and will it."

    Stop-Process -Id $process.id -Force
    #Write-Output "Langflow process was killed."
} else {
    #Write-Output "Can not find langflow process"
}

Write-Host "Please keep this window open. It will open the browser with  http://127.0.0.1:8979 automatically ...."

$process = Get-WmiObject -Class Win32_Process | Where-Object { $_.ProcessName -like "*Genie*.exe" }

if ($process) {
    Write-Host "C++ Genie service ready"
} else {
    Write-Host "can not find C++ Genie service. Please run Start_GenieAPIService.bat to start genie serve!"
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
                    Write-Host "Server is ready. Opening the browser..."
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
        Write-Host "Failed to detect the server readiness after $maxAttempts attempts."
    }
}

langflow run --host 0.0.0.0 --port 8979
