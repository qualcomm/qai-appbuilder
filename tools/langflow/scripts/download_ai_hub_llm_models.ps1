# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$userProfilePath = $env:USERPROFILE

function Download-And-Extract-File {
    param (
        [Parameter(Mandatory = $true)]
        [string]$Url,
        
        [Parameter(Mandatory = $true)]
        [string]$CachePath,
        
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath
    )

    # Set retry parameters
    $max_attempts = 5
    $attempt = 0
    $success = $false
    
    # Download file (with retry mechanism and file size verification)
    do {
        $attempt++
        Write-Host "Downloading file (Attempt: $attempt/$max_attempts)..."
        
        try {
            # Get remote file size
            Write-Host "Retrieving remote file size..."
            $request = [System.Net.WebRequest]::Create($Url)
            $request.Method = "HEAD"
            $response = $request.GetResponse()
            $remote_file_size = $response.ContentLength
            $response.Close()
            Write-Host "Remote file size: $($remote_file_size / 1MB) MB"
            
            # Check if cached file exists
            if (Test-Path -Path $CachePath) {
                $local_file_size = (Get-Item $CachePath).Length
                Write-Host "Existing cached file found, size: $($local_file_size / 1MB) MB"
                
                # Verify cached file size
                if ($local_file_size -eq $remote_file_size) {
                    Write-Host "Cached file size matches remote file, skipping download"
                    $success = $true
                } else {
                    Write-Host "Cached file size mismatch, deleting and redownloading..."
                    Remove-Item -Path $CachePath -Force
                }
            }
            
            # Download if necessary
            if (-not $success) {
                # Record download start time
                $start_time = Get-Date
                
                # Execute download
                Write-Host "Starting download..."
                Invoke-WebRequest -Uri $Url -OutFile $CachePath -UseBasicParsing
                
                # Calculate download duration
                $duration = (Get-Date) - $start_time
                Write-Host "Download completed, duration: $($duration.TotalSeconds) seconds"
                
                # Verify downloaded file size
                $local_file_size = (Get-Item $CachePath).Length
                
                if ($local_file_size -eq $remote_file_size) {
                    Write-Host "File size verification passed: $($local_file_size / 1MB) MB"
                    $success = $true
                } else {
                    Write-Host "File size verification failed!"
                    Write-Host "  Local file: $($local_file_size / 1MB) MB"
                    Write-Host "  Remote file: $($remote_file_size / 1MB) MB"
                    throw "Downloaded file is incomplete"
                }
            }
        } catch {
            Write-Host "Download failed: $_"
            
            if ($attempt -lt $max_attempts) {
                # Calculate wait time (exponential backoff)
                $wait_seconds = [Math]::Pow(2, $attempt)
                Write-Host "Will retry in $wait_seconds seconds..."
                Start-Sleep -Seconds $wait_seconds
            }
        }
    } while (-not $success -and $attempt -lt $max_attempts)
    
    # If download was successful, extract the file
    if ($success) {
        try {
            Write-Host "Starting extraction to $DestinationPath..."
            Expand-Archive -Path $CachePath -DestinationPath $DestinationPath -Force -ErrorAction Stop
            Write-Host "Extraction completed successfully!"
            return $true
        } catch {
            Write-Host "Extraction failed: $_"
            return $false
        }
    } else {
        return $false
    }
}

Function Download_ai_hub_llm_models {
    param()
    process {
        $ibm_grantite_url =  "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct_quantized/v1/snapdragon_x_elite/models.zip"
        $ibm_grantite_zip_path = 'ibm_granite_v3_1_8b_instruct_quantized.zip'
        $ibm_grantite_cache_path = "..\cache\ibm_granite_v3_1_8b_instruct_quantized\$ibm_grantite_zip_path"
        $ibm_grantite_destination_path = "..\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\IBM-Granite-v3.1-8B"

        $result = Download-And-Extract-File -Url $ibm_grantite_url -CachePath $ibm_grantite_cache_path -DestinationPath $ibm_grantite_destination_path

        # Check if the entire process was successful
        if ($result) {
            Write-Host "Model downloaded and extracted successfully!"
        } else {
            Write-Host "Failed to download or extract the model."
            exit 1
        }


        if($false)
        {

            $phi_url = 'https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct_quantized/v1/snapdragon_x_elite/models.zip'
            $phi_zip_path = 'phi_3_5_mini_instruct_quantized.zip'
            $phi_cache_path = "..\cache\phi_3_5_mini_instruct_quantized\$phi_zip_path"
            $phi_destination_path = "..\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\Phi-3.5-mini"

            $result = Download-And-Extract-File -Url $phi_url -CachePath $phi_cache_path -DestinationPath $phi_destination_path

            # Check if the entire process was successful
            if ($result) {
                Write-Host "Model downloaded and extracted successfully!"
            } else {
                Write-Host "Failed to download or extract the model."
                exit 1
            }
        }

    }
}


