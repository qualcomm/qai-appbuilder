#==============================================================================
#
#  Copyright (c) 2025 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
#==============================================================================


$userProfilePath = $env:USERPROFILE

Function download_install_python {
    param()
    process {
        # Check python3.12 status. Install it if it is not found
        $python312 = $false

        try {
            $pythonExes = @("py.exe", "python.exe", "python3.12.exe", "python3.exe")
            foreach ($exe in $pythonExes) {
                if (Get-Command $exe -ErrorAction SilentlyContinue) {
                    $pythonVersion = & $exe --version 2>&1
                    if ($pythonVersion -match "Python 3\.12\.\d+") {
                        $python312 = $exe
                        break
                    }
                }
            }
        }
        catch {
            Write-Output "Can not find python. Will download and install it"
        }

        if ($python312 -eq $false) {    
            $python312URL = 'https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe'
            # Need to check whether python-3.12.0-amd64.exe exist or not.
            $filePath = Join-Path $PSScriptRoot (Split-Path -Leaf $python312URL)
            $downloadSuccess = $false
            $maxRetries = 3

            for ($retryCount = 1; $retryCount -le $maxRetries; $retryCount++) {
                try {
                    Invoke-WebRequest -Uri $python312URL -OutFile $filePath
                    Write-Host "Downloaded: $filePath"
                    #check the donwloaded file again.
                    $fileHash = Get-FileHash -Algorithm MD5 -Path $filePath
                    $expectedHash = "32AB6A1058DFBDE76951B7AA7C2335A6"

                    if ($fileHash.Hash -eq $expectedHash) {
                        $downloadSuccess = $true
                        break
                    }
                }
                catch {
                    Write-Host "Error downloading: $_"
                    if ($retryCount -lt $maxRetries) {
                        Write-Host "Retrying download... Attempt $retryCount of $maxRetries"
                    }
                    else {
                        Write-Host "Failed to download after $maxRetries attempts."
                        return $false
                    }
                }
            }

            if ($downloadSuccess) {
                $process = Start-Process -FilePath $filePath -ArgumentList "/silent InstallAllUsers=1 PrependPath=1" -NoNewWindow -PassThru
                $process.WaitForExit()
                $exitCode = $process.ExitCode
                Write-Output "Exit Code: $exitCode"
                if ($exitCode -ne 0) {
                    Write-Output "Install python error. Please try to install python3.12 manually or re-run this script"
                    return $false
                }

            }
        }


        # Attempt to remove the python.exe and python3.exe from the WindowsApps directory in the user's AppData
        try {
            # Define the path to python.exe in the WindowsApps directory
            $pythonExePath = Join-Path -Path $userProfilePath -ChildPath "AppData\Local\Microsoft\WindowsApps\python.exe"
            # Define the path to python3.exe in the WindowsApps directory
            $python3ExePath = Join-Path -Path $userProfilePath -ChildPath "AppData\Local\Microsoft\WindowsApps\python3.exe"

            # Check if python.exe exists and remove it if it does
            if (Test-Path -Path $pythonExePath) {
                Remove-Item -Path $pythonExePath -Force
            }
            # Check if python3.exe exists and remove it if it does
            if (Test-Path -Path $python3ExePath) {
                Remove-Item -Path $python3ExePath -Force
            }
        }
        catch {
            # Output an error message if an exception occurs during the removal process
            Write-Output "删除文件时发生错误: $_"
        }

        # Define the paths you want to add
        $targetPaths = "$userProfilePath\AppData\Local\Programs\Python\Python312\Scripts\;$userProfilePath\AppData\Local\Programs\Python\Python312\;$userProfilePath\AppData\Local\Programs\Python\Launcher\;$userProfilePath\AppData\Local\Programs\Ollama" -split ';' | ForEach-Object { $_.Trim() }

        # Get the existing system path
        $existingPaths = [Environment]::GetEnvironmentVariable("PATH", "User") -split ';'

        foreach ($targetPath in $targetPaths) {
            if ($existingPaths -notcontains $targetPath) {
                # If not, add the target path to the existing paths
                $newPath = "$targetPath;" + [Environment]::GetEnvironmentVariable("PATH", "User")
                [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
                Write-Host "The path $targetPath has been permanently added to the system environment variable PATH."
            }
            else {
                Write-Host "The path $targetPath is already in the system environment variable PATH."
            }
        }

        $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH", "User")
    }

}

