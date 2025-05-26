#==============================================================================
#
#  Copyright (c) 2025 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
#==============================================================================

function Utils_Copy_WithProgress {
    param (
        [string]$source,
        [string]$destination,
        [string]$filterpattern
    )

    # Get all files and directories from the source
    $items = Get-ChildItem -Path $source -Recurse

    # Initialize progress bar
    $totalItems = $items.Count
    $currentItem = 0

    foreach ($item in $items) {
        $currentItem++
        $percentComplete = ($currentItem / $totalItems) * 100

        # Update progress bar
        Write-Progress -Activity "Copying files" -Status "Copying $($item.FullName)" -PercentComplete $percentComplete

        # Construct the destination path
        $destinationItemPath = $item.FullName -replace [regex]::Escape($source), $destination

        # Check if the item is under a "models" directory
        if ($item.FullName -like "*$filterpattern*" -and $item.FullName -notlike "*modelslvm*") {
            #Write-Host "Skipping item: $($item.FullName)"
            continue
        }

        # Check if the item is a directory
        if ($item.PSIsContainer) {
            # Create the directory if it doesn't exist
            if (-Not (Test-Path -Path $destinationItemPath)) {
                New-Item -ItemType Directory -Path $destinationItemPath | Out-Null
            }
        } else {
            # Ensure the destination path is not the same as the source path
            if ($item.FullName -ne $destinationItemPath) {
                # Copy the file
                Copy-Item -Path $item.FullName -Destination $destinationItemPath -Force
            } else {
                Write-Host "Skipping file: $($item.FullName) - Destination is the same as source."
            }
        }
    }

    # Complete progress bar
    Write-Progress -Activity "Copying files" -Completed
}

function Utils_Delete_Folder_With_Retry {
    param (
        [string]$folderPath,
        [int]$maxAttempts = 5
    )

    $attempt = 0
    
    while (Test-Path $folderPath -PathType Container) {
        $attempt++
        if($maxAttempts -lt  $attempt )
        {
            break;
        }
        
        Remove-Item -Path $folderPath -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $folderPath -PathType Container)
        {
            Start-Sleep -Seconds 1 
        }
        else {
           break
        }
        
    }  

    if (Test-Path $folderPath -PathType Container) {
        Write-Host "Error: Failed to delete $folderPath after $maxAttempts attempts." -ForegroundColor Red
        return $false
    }

    return $true
}
