# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# Check ollama status. Install it if it is not found
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Output "ollama is installed."
}
else {


    $ollamaURL = 'https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe'


    $expectedHash = "FFC0EBBD1784C35AA3C73AEE3639396A"
    $fileHash = Get-FileHash -Algorithm MD5 -Path (Split-Path -Leaf $ollamaURL)


    if ($fileHash.Hash -eq $expectedHash) {
        Write-Output "Already downloaded ollama."
    } else {
        Write-Output "Start to download the ollama...."
    
        try {
            # Start the download process with a progress bar
            $response = $null
            $webRequestJob = Start-Job -ScriptBlock {
                param ($url)
                Invoke-WebRequest -Uri $url -Method Head -ErrorAction Stop
            } -ArgumentList $ollamaURL

            # Wait for the job to complete
            Wait-Job -Job $webRequestJob

            # Retrieve the job results
            $response = Receive-Job -Job $webRequestJob
            Remove-Job -Job $webRequestJob

            if ($response.StatusCode -eq 200) {
                Write-Host "The resource is available. Starting download..."
                Invoke-WebRequest -Uri $ollamaURL -OutFile (Join-Path $PSScriptRoot "OllamaSetup.exe")
            } else {
                Write-Host "Ollama download failed. Press any key to exit..."
                [void][System.Console]::ReadKey($true)
            }
        } catch {
            Write-Host "An error occurred: $_"
            [void][System.Console]::ReadKey($true)
        }
    }

    #Start-Process -FilePath (Split-Path -Leaf $ollamaURL) -ArgumentList "/silent" -NoNewWindow -Wait
    $filePath = Join-Path $PSScriptRoot (Split-Path -Leaf $ollamaURL)
    Write-Host "The ollama.exe path is: $filePath"
    $process = Start-Process -FilePath $filePath -ArgumentList "/silent" -NoNewWindow -PassThru
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    Write-Output "Exit Code: $exitCode"


    #Start-Process -FilePath (Split-Path -Leaf $ollamaURL) -ArgumentList "/quiet InstallAllUsers=1" -Wait

    }

# Check if the model nomic-embed-text is installed
$match = ollama list | Select-String -Pattern "nomic-embed-text"

if ($match) {
    Write-Host "The model nomic-embed-text was found. No need to install"
} else {
    # Initialize the counter
    $attempts = 0

    do {
        $result = & ollama pull nomic-embed-text
        if ($LASTEXITCODE -eq 0) {
            Write-Output "ollama pull nomic-embed-text succeeded."
            break
        } else {
            Write-Output "ollama pull nomic-embed-text failed. Retrying..."
            Start-Sleep -Seconds 1  # Wait for 1 second before retrying
            $attempts++
        }
        
        # Check if the number of attempts has reached 5
        if ($attempts -ge 5) {
            Write-Host "ollama pull nomic-embed-text failed after 5 attempts." -ForegroundColor Red
            exit 1
        }
    } while ($true)
}

Write-Host "Install Ollama and embeding model successfully....."