
# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$userProfilePath = $env:USERPROFILE



function Create-FolderIfNotExists {
    param (
        [string]$folderPath
    )
    # Check if the folder exists
    if (-not (Test-Path -Path $folderPath -PathType Container)) {
        try {
            # Create the folder if it doesn't exist
            New-Item -ItemType Directory -Path $folderPath -Force | Out-Null
            Write-Host "The folder $folderPath has been created successfully."
        }
        catch {
            Write-Error "An error occurred while creating the folder: $_"
        }
    }
    else {
        Write-Host "The folder $folderPath already exists."
    }
}




#Create the log folder for log 
# Example usage of the function
$path1 = "$userProfilePath\code\qualcomm\GenieWebUI\log"
Create-FolderIfNotExists -folderPath $path1

$path2 = "$userProfilePath\code\qualcomm\langflow_cv\log"
Create-FolderIfNotExists -folderPath $path2
    

$process= Get-Process | Where-Object { $_.ProcessName -like "*uv*"}
if($process){
    #Write-Output "Found uv process and will it."

    Stop-Process -Id $process.id -Force
    Write-Output "Langflow process was killed."
} else {
    #Write-Output "Can not find uv process"
}

$process= Get-Process | Where-Object { $_.ProcessName -like "*langflow*"}
if($process){
    #Write-Output "Found langflow process and will it."

    Stop-Process -Id $process.id -Force
    #Write-Output "Langflow process was killed."
} else {
    #Write-Output "Can not find langflow process"
}



write-output $userProfilePath

# Find the Python process running the Genie script
$processes = Get-WmiObject -Class Win32_Process | Where-Object { $_.ProcessName -like "python.exe" -and $_.CommandLine -like "*Genie*.py" }
# Check if any matching processes are found
if ($processes) {
    foreach ($process in $processes) {
        try {
            # Try to terminate the process using Stop-Process
            Stop-Process -Id $process.ProcessId -Force
            Write-Output "Clean env and prepare to start genie service ... "
        }
        catch {
            Write - Error "Failed to terminate process with ID $($process.ProcessId). Error: $($_.Exception.Message)"
            exit 1 
        }
    }
}
else {
    Write-Output "Prepare to start genie service ..."
}

# 提示用户保持窗口打开，并告知将自动打开浏览器访问 127.0.0.1:7860
Write-Host "Please keep this window open. It will open the browser with  http://127.0.0.1:7860 automatically ...."



# Define the script block
$scriptBlock = {

    $timestamp = Get-Date -Format "yyyy_MM_dd_HH_mm_ss"
    $logFileName = "log_$timestamp.txt"
    $userProfilePath = $env:USERPROFILE
    pushd "$userProfilePath\code\qualcomm\ai-engine-direct-helper\samples";& "$userProfilePath\code\myenvs\clean-py3.12\Scripts\Activate.ps1";python genie\python\GenieAPIService.py > "$userProfilePath\code\qualcomm\GenieWebUI\log\$logFileName"  2>&1
    
}

# Start the job
$job = Start-Job -ScriptBlock $scriptBlock


# 等待一段时间以确保进程启动
Start-Sleep -Seconds 5

# 尝试查找进程
$process = Get-WmiObject -Class Win32_Process | Where-Object { $_.ProcessName -like "python.exe" -and $_.CommandLine -like "*Genie*.py" }

if ($process) {
    Write-Host "Python Genie service ready"
} else {
    Write-Host "can not find Python Genie service"
    # 获取作业错误信息
    $errors = Receive-Job -Job $job -ErrorAction SilentlyContinue
    if ($errors) {
        Write-Host "error info:"
        $errors | ForEach-Object { Write-Host $_.Exception.Message }
    }
}


    
$newFolderPath = Join-Path -Path $userProfilePath -ChildPath "code\myenvs"
& "$newFolderPath\langflow-new-py3.12\Scripts\Activate.ps1"


Start-Job -ScriptBlock {
    $maxAttempts = 60
    $attempt = 0
    $interval = 2  # seconds
    $urlOpened = $false

    while ($attempt -lt $maxAttempts) {
        try {
            $socket = New-Object System.Net.Sockets.TcpClient
            $socket.Connect("127.0.0.1", 7860)
            # 服务器连接成功后，额外等待一小段时间确保服务器真正就绪
            Start-Sleep -Seconds 1

            # 再次尝试连接确认
            try {
                $socket = New-Object System.Net.Sockets.TcpClient
                $socket.Connect("127.0.0.1", 7860)
                if (-not $urlOpened) {
                    Write-Host "Server is ready. Opening the browser..."
                    Start-Process -FilePath "http://127.0.0.1:7860"
                    $urlOpened = $true
                    return
                }
            }
            catch {
                # 第二次连接失败，继续等待
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
    
        


uv run langflow run --host 0.0.0.0 --port 7860



