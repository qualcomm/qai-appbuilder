

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
$path1 = "..\code\qualcomm\GenieWebUI\log"
Create-FolderIfNotExists -folderPath $path1

$path2 = "..\code\qualcomm\langflow_cv\log"
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


# 提示用户保持窗口打开，并告知将自动打开浏览器访问 127.0.0.1:8979
Write-Host "Please keep this window open. It will open the browser with  http://127.0.0.1:8979 automatically ...."


    
$newFolderPath = "..\code\myenvs"
& "$newFolderPath\langflow-new-py3.12\Scripts\Activate.ps1"


Start-Job -ScriptBlock {
    $maxAttempts = 60
    $attempt = 0
    $interval = 2  # seconds
    $urlOpened = $false

    while ($attempt -lt $maxAttempts) {
        try {
            $socket = New-Object System.Net.Sockets.TcpClient
            $socket.Connect("127.0.0.1", 8979)
            # 服务器连接成功后，额外等待一小段时间确保服务器真正就绪
            Start-Sleep -Seconds 1

            # 再次尝试连接确认
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
    
        


uv run langflow run --host 0.0.0.0 --port 8979



