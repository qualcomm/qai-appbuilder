# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

$vsStudioUrl="https://aka.ms/vs/17/release/vs_BuildTools.exe"
$vsStudioDownloadPath=".\tools\vs_BuildTools.exe"

Function install_VS_Studio {
    process {
        # Install the Visual Studio
        Start-Process -FilePath $vsStudioDownloadPath -ArgumentList " --passive --wait  --add Microsoft.VisualStudio.Component.Windows11SDK.22621  --add Microsoft.VisualStudio.Component.VC.14.34.17.4.x86.x64  --add Microsoft.VisualStudio.Component.VC.CMake.Project" -Wait -PassThru   
    }
}

Function download_install_VS_Studio {
    process {
        Write-Output "Downloading the Visual Studio..." 
        try {
            Invoke-WebRequest -Uri $vsStudioUrl -OutFile $vsStudioDownloadPath
            # Checking for successful download
            Write-Output "vs_BuildTools.exe is downloaded at : $vsStudioDownloadPath" 
            Write-Output "Installing Visual Studio..."
            if (install_VS_Studio) {
                Write-Output "Visual Studio installed successfully." 
            }
            else{
                Write-Output "Visual Studio installation failed from: $vsStudioDownloadPath"  
            }
        }
        catch {
            Write-Output "Visual Studio download or install failed. Downloaded the Visual Studio from: $vsStudioUrl and install." 
        }
    }
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


