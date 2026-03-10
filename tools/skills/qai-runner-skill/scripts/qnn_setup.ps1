# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

<#  
    The qnn_setup.ps1 PowerShell script automatesthe setup process for Qualcomm's AI Engine Direct by downloading and installing necessary components, including Python, ONNX models, QNN SDK, and various dependencies.
    It creates and activates a virtual environment, upgrades pip, and installs required Python packages. 
    The function also runs scripts to check and ensure all dependencies are correctly set up, providing a complete and successful installation for AI Engine Direct QNN. 
    By default, $rootDirPath is set to C:\WoS_AI, where all files will be downloaded and the Python environment will be created.
	
    Note: Users can modify values such as rootDirPath, QNN SDK version, etc, if desired
#>



# Define QNN SDK version (at the time of writing tutorials). Users can change this version if they have downloaded a different version of QNN SDK.
$QNN_SDK_VERSION = "2.43.0.260128"

# Define URLs for dependencies

# Python 3.10.4 dependency for QNN SDK.
$pythonUrl = "https://www.python.org/ftp/python/3.10.4/python-3.10.4-amd64.exe"

# Cmake 3.30.4 url
$cmakeUrl = "https://github.com/Kitware/CMake/releases/download/v3.30.4/cmake-3.30.4-windows-arm64.msi"

$pyscriptUrl = "qnn_setup.ps1 https://raw.githubusercontent.com/quic/wos-ai/refs/heads/main/Scripts/setup_artifact_download.py"

# QNN SDK download link for converting, generating, and executing the model on HTP (NPU) backend
$aIEngineSdkUrl = "https://apigwx-aws.qualcomm.com/qsc/public/v1/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/$QNN_SDK_VERSION/v$QNN_SDK_VERSION.zip"

<# Required files 
    - License             : License document
#>
$licenseUrl        = "https://raw.githubusercontent.com/quic/wos-ai/refs/heads/main/LICENSE"

# gen_qnn_ctx_gen_ctx python script
$genQnnCtxOnnxUrl    = "https://raw.githubusercontent.com/microsoft/onnxruntime/refs/heads/main/onnxruntime/python/tools/qnn/gen_qnn_ctx_onnx_model.py"

<#  Artifacts for tutorials, including:
    - io_utils.py: Utility file for preprocessing images and postprocessing to get top 5 predictions.
#>
# Define the URL of the file to download
$io_utilsUrl                = "https://raw.githubusercontent.com/quic/wos-ai/refs/heads/main/Artifacts/io_utils.py"

# ONNX model file for image prediction used in tutorials
$modelUrl = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/apidoc/mobilenet_v2.onnx"

# Visual Studio dependency for compiling and converting ONNX model to C++ & binary, used for generating model.dll file
$vsStudioUrl = "https://download.visualstudio.microsoft.com/download/pr/7593f7f0-1b5b-43e1-b0a4-cceb004343ca/09b5b10b7305ae76337646f7570aaba52efd149b2fed382fdd9be2914f88a9d0/vs_Enterprise.exe"

# QNN SDK installation path
$aIEngineSdkInstallPath = "C:\Qualcomm\AIStack\QAIRT"

# Define the python installation path.
# Retrieves the value of the Username
$username = (Get-ChildItem Env:\Username).value
$pythonInstallPath = "C:\Users\$username\AppData\Local\Programs\Python\Python310"
$pythonScriptsPath = $pythonInstallPath+"\Scripts"

# Define the cmake installation path.
$cmakeInstallPath = "C:\Program Files\CMake"

# Define Python QAIRT_VENV environment path in the root directory. This environment will be used to install QNN SDK dependencies and tutorial-related dependencies.
$QAIRT_VENV_Path = "Python_Venv\QAIRT_VENV"

# Define Mobilenet path in the root directory.
$Mobilenet_Folder_path = "Models\Mobilenet_V2"


$vsInstallerPath = "C:\VS\Common7\Tools\Launch-VsDevShell.ps1"
$SUGGESTED_VS_BUILDTOOLS_VERSION = "14.34"
$SUGGESTED_WINSDK_VERSION = "10.0.22621"
$SUGGESTED_VC_VERSION = "19.34"

$global:CHECK_RESULT = 1
$global:tools = @{}
$global:tools.add( 'vswhere', "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe" )


############################ Function ##################################

Function Set_Variables {
    param (
        [string]$rootDirPath = "C:\WoS_AI"
    )
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $rootDirPath)) {
        New-Item -ItemType Directory -Path $rootDirPath
    }
    Set-Location -Path $rootDirPath
    # Define download directory inside the working directory for downloading all dependency files and SDK.
    $global:pyscriptDownloadPath = "$rootDirPath\setup_artifact_download.py"
    $global:downloadDirPath = "$rootDirPath\Downloads"
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $downloadDirPath)) {
        New-Item -ItemType Directory -Path $downloadDirPath
    }
    # Define the path where the installer will be downloaded.
    $global:pythonDownloaderPath = "$downloadDirPath\python-3.10.4-amd64.exe"
    $global:cmakeDownloaderPath  = "$downloadDirPath\cmake-3.30.4-windows-arm64.msi"
    $global:vsStudioDownloadPath = "$downloadDirPath\vs_Enterprise.exe"

    # Define the SDK download path.
    $global:aIEngineSdkDownloadPath     = "$downloadDirPath\qairt\$QNN_SDK_VERSION"

    # Define the license download path.
    $global:lincensePath      = "$rootDirPath\License"

    $global:debugFolder    = "$rootDirPath\Debug_Logs"
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $debugFolder)) {
        New-Item -ItemType Directory -Path $debugFolder
    }
    # Define mobilenet folder for mobilenet artifacts.
    $global:mobilenetFolder = "$rootDirPath\$Mobilenet_Folder_path"
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $mobilenetFolder)) {
        New-Item -ItemType Directory -Path $mobilenetFolder
    }
    # Define the artifacts download path.
    $global:io_utilsPath                = "$mobilenetFolder\io_utils.py"

    # Define the mobilenet model download path.
    $global:modelFilePath               = "$mobilenetFolder\mobilenet_v2.onnx"

    # Define folder path for mobilenet qnn_artifacts.
    $global:qnnartifactsPath            = "$mobilenetFolder\QNN_Artifacts"
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $qnnartifactsPath )) {
        New-Item -ItemType Directory -Path $qnnartifactsPath
    }
    
    # Define folder path for QNN Dependencies.
    $global:qnndependenciesPath         = "$rootDirPath\Models\QNN_Dependencies"
    # Create the Root folder if it doesn't exist
    if (-Not (Test-Path $qnndependenciesPath )) {
        New-Item -ItemType Directory -Path $qnndependenciesPath
    }
    #Define the gen_qnn_ctx_onnx download path
    $global:gen_qnn_ctx_onnx_FilePath           =  "$qnndependenciesPath\gen_qnn_ctx_onnx_model.py"
}

Function Show-Progress {
    param (
        [int]$percentComplete,
        [int]$totalPercent
    )
    $progressBar = ""
    $progressWidth = 100
    $progress = [math]::Round((($percentComplete/$totalPercent)*100) / 100 * $progressWidth)
    for ($i = 0; $i -lt $progressWidth; $i++) {
        if ($i -lt $progress) {
            $progressBar += "#"
        } else {
            $progressBar += "-"
        }
    }
    # Write-Progress -Activity "Progress" -Status "$percentComplete% Complete" -PercentComplete $percentComplete
    Write-Host "[$progressBar] ($percentComplete/$totalPercent) Setup Complete"
}

Function install_VS_Studio {
    param()
    process {
        # Install the VS Studio
        Start-Process -FilePath $vsStudioDownloadPath -ArgumentList "--installPath C:\VS --passive --wait --add Microsoft.VisualStudio.Workload.NativeDesktop --includeRecommended --add Microsoft.VisualStudio.Component.VC.14.34.17.4.x86.x64 --add Microsoft.VisualStudio.Component.VC.14.34.17.4.ARM64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621 --add Microsoft.VisualStudio.Component.VC.CMake.Project --add Microsoft.VisualStudio.Component.VC.Llvm.Clang --add Microsoft.VisualStudio.Component.VC.Llvm.ClangToolset" -Wait -PassThru
        # Check if the VS Studio
        check_MSVC_components_version
    }
}


Function show_recommended_version_message {
    param (
        [String] $SuggestVersion,
        [String] $FoundVersion,
        [String] $SoftwareName
    )
    process {
        Write-Warning "The version of $SoftwareName $FoundVersion found has not been validated. Recommended to use known stable $SoftwareName version $SuggestVersion"
    }
}

Function show_required_version_message {
    param (
        [String] $RequiredVersion,
        [String] $FoundVersion,
        [String] $SoftwareName
    )
    process {
        Write-Host "ERROR: Require $SoftwareName version $RequiredVersion. Found $SoftwareName version $FoundVersion" -ForegroundColor Red
    }
}


Function compare_version {
    param (
        [String] $TargetVersion,
        [String] $FoundVersion,
        [String] $SoftwareName
    )
    process {
        if ( (([version]$FoundVersion).Major -eq ([version]$TargetVersion).Major) -and (([version]$FoundVersion).Minor -eq ([version]$TargetVersion).Minor) ) { }
        elseif ( (([version]$FoundVersion).Major -ge ([version]$TargetVersion).Major) -and (([version]$FoundVersion).Minor -ge ([version]$TargetVersion).Minor) ) {
            show_recommended_version_message $TargetVersion $FoundVersion $SoftwareName
        }
        else {
            show_required_version_message $TargetVersion $FoundVersion $SoftwareName
            $global:CHECK_RESULT = 0
        }
    }
}

Function locate_prerequisite_tools_path {
    param ()
    process {
        # Get and Locate VSWhere
        if (!(Test-Path $global:tools['vswhere'])) {
            Write-Host "No Visual Studio Instance(s) Detected, Please Refer To The Product Documentation For Details" -ForegroundColor Red
        }
    }
}

Function detect_VS_instance {
    param ()
    process {
        locate_prerequisite_tools_path

        $INSTALLED_VS_VERSION = & $global:tools['vswhere'] -latest -property installationVersion
        $INSTALLED_PATH = & $global:tools['vswhere'] -latest -property installationPath
        $productId = & $global:tools['vswhere'] -latest -property productId

        return $productId, $INSTALLED_PATH, $INSTALLED_VS_VERSION
    }
}

Function check_VS_BuildTools_version {
    param (
        [String] $SuggestVersion = $SUGGESTED_VS_BUILDTOOLS_VERSION
    )
    process {
        $INSTALLED_PATH = & $global:tools['vswhere'] -latest -property installationPath
        $version_file_path = Join-Path $INSTALLED_PATH "VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt"
        if (Test-Path $version_file_path) {
            $INSTALLED_VS_BUILDTOOLS_VERSION = Get-Content $version_file_path
            compare_version $SuggestVersion $INSTALLED_VS_BUILDTOOLS_VERSION "VS BuildTools"
            return $INSTALLED_VS_BUILDTOOLS_VERSION
        }
        else {
            Write-Error "VS BuildTools not installed"
            $global:CHECK_RESULT = 0
        }
        return "Not Installed"
    }
}

Function check_WinSDK_version {
    param (
        [String] $SuggestVersion = $SUGGESTED_WINSDK_VERSION
    )
    process {
        $INSTALLED_WINSDK_VERSION = Get-ItemPropertyValue -Path 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Microsoft SDKs\Windows\v10.0' -Name ProductVersion
        if($?) {
            compare_version $SuggestVersion $INSTALLED_WINSDK_VERSION "Windows SDK"
            return $INSTALLED_WINSDK_VERSION
        }
        else {
            Write-Error "Windows SDK not installed"
            $global:CHECK_RESULT = 0
        }
        return "Not Installed"
    }
}

Function check_VC_version {
    param (
        [String] $VsInstallLocation,
        [String] $BuildToolVersion,
        [String] $Arch,
        [String] $SuggestVersion = $SUGGESTED_VC_VERSION
    )
    process {
        $VcExecutable = Join-Path $VsInstallLocation "VC\Tools\MSVC\" | Join-Path -ChildPath $BuildToolVersion | Join-Path -ChildPath "bin\Hostx64" | Join-Path -ChildPath $Arch | Join-Path -ChildPath "cl.exe"

        if(Test-Path $VcExecutable) {
            #execute $VcExecutable and retrieve stderr since version is in it.
            $process_alloutput = & "$VcExecutable" 2>&1
            $process_stderror = $process_alloutput | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }
            $CMD = $process_stderror | Out-String | select-string "Version\s+(\d+\.\d+\.\d+)" # The software version is output in STDERR
            $INSTALLED_VC_VERSION = $CMD.matches.groups[1].value
            if($INSTALLED_VC_VERSION) {
                compare_version $SuggestVersion $INSTALLED_VC_VERSION ("Visual C++(" + $Arch + ")")
                return $INSTALLED_VC_VERSION
            }
            else {
                Write-Error "Visual C++ not installed"
                $global:CHECK_RESULT = 0
            }
        }
        return "Not Installed"
    }
}

Function check_MSVC_components_version {
    param ()
    process {
        $check_result = @()
        $productId, $vs_install_path, $vs_installed_version = detect_VS_instance
        if ($productId) {
            $check_result += [pscustomobject]@{Name = "Visual Studio"; Version = $vs_installed_version}
        }
        else {
            $check_result += [pscustomobject]@{Name = "Visual Studio"; Version = "Not Installed"}
            $global:CHECK_RESULT = 0
        }
        $buildtools_version = check_VS_BuildTools_version
        $check_result += [pscustomobject]@{Name = "VS Build Tools"; Version = $buildtools_version}
        $check_result += [pscustomobject]@{Name = "Visual C++(x86)"; Version = check_VC_version $vs_install_path $buildtools_version "x64"}
        $check_result += [pscustomobject]@{Name = "Visual C++(arm64)"; Version = check_VC_version $vs_install_path $buildtools_version "arm64"}
        $check_result += [pscustomobject]@{Name = "Windows SDK"; Version = check_WinSDK_version}
        Write-Host ($check_result | Format-Table| Out-String).Trim()
    }
}

Function install_python {
    param()
    process {
        # Install Python
        Start-Process -FilePath $pythonDownloaderPath -ArgumentList "/quiet InstallAllUsers=1 TargetDir=$pythonInstallPath" -Wait
        # Check if Python was installed successfully
        if (Test-Path "$pythonInstallPath\python.exe") {
            Write-Output "Python installed successfully."
            # Get the current PATH environment variable
            $envPath = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::User)

            # Add the new paths if they are not already in the PATH
            if ($envPath -notlike "*$pythonScriptsPath*") {
                $envPath = "$pythonScriptsPath;$pythonInstallPath;$envPath"
                [System.Environment]::SetEnvironmentVariable("Path", $envPath, [System.EnvironmentVariableTarget]::User)
            }

            # Refresh environment variables
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::Machine) + ";" + [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::User)

            return $true
        }
        return $false
    }
}

Function install_cmake {
    param()
    process {
        # Install CMake
        Start-Process msiexec.exe -ArgumentList "/i", $cmakeDownloaderPath, "/quiet", "/norestart" -Wait
        # Check if CMake was installed successfully
        if (Test-Path "$cmakeInstallPath\bin\cmake.exe") {
            Write-Output "CMake installed successfully."
            # Get the current PATH environment variable
            $envPath = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::User)

            # Add the new paths if they are not already in the PATH
            if ($envPath -notlike "*$cmakeInstallPath\bin*") {
                $envPath = "$cmakeInstallPath\bin;$envPath"
                [System.Environment]::SetEnvironmentVariable("Path", $envPath, [System.EnvironmentVariableTarget]::User)
            }

            # Refresh environment variables
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::Machine) + ";" + [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::User)
            return $true
        }
        return $false
    }
}

Function download_file {
    param (
        [string]$url,
        [string]$downloadfile
    )
    process {
        try {
            Invoke-WebRequest -Uri $url -OutFile $downloadfile
            return $true
        }
        catch {
            return $false
        }
    }
}
 
Function download_and_extract {
    param (
        [string]$artifactsUrl,
        [string]$rootDirPath
    )
    process {
        $zipFilePath = "$rootDirPath\downloaded.zip"
        # Download the ZIP file
        #python .\setup_artifact_download.py -url $artifactsUrl -outputfile $zipFilePath
	curl.exe -L -o $zipFilePath $artifactsUrl

         # Extract the ZIP file
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipFilePath, $rootDirPath)
	return $true
    }  
}
############################## Main code ##################################


Function download_install_python {
    param()
    process {
        # Check if python already installed
        # If Yes
        if (Test-Path "$pythonInstallPath\python.exe") {
            Write-Output "Python already installed."
        }
        # Else downloading and installing python
        else{
            Write-Output "Downloading the python file ..." 
            $result = download_file -url $pythonUrl -downloadfile $pythonDownloaderPath
            # Checking for successful download
            if ($result) {
                Write-Output "Python File is downloaded at : $pythonDownloaderPath"
                Write-Output "Installing python..."
                if (install_python) {
                    Write-Output "Python 3.10.4 installed successfully." 
                }
                else {
                    Write-Output "Python installation failed.. Please installed python 3.10.4 from : $pythonDownloaderPath"  
                }
            } 
            else{
                Write-Output "Python download failed. Download the python file from : $pythonUrl and install." 
            }
        }
    }
}



Function download_install_cmake {
    param()
    process {
        # Checking if CMake already installed
        # If yes
        if (Test-Path "$cmakeInstallPath\bin\cmake.exe") {
            Write-Output "CMake already installed."
        }
        # Else downloading and installing CMake
        else {
            Write-Output "Downloading the CMake file ..."
            $result = download_file -url $cmakeUrl -downloadfile $cmakeDownloaderPath
            # Checking for successful download
            if ($result) {
                Write-Output "CMake file is downloaded at : $cmakeDownloaderPath"
                Write-Output "Installing CMake..."
                if (install_cmake) {
                    Write-Output "CMake 3.30.4 installed successfully."
                }
                else {
                    Write-Output "CMake installation failed. Please install CMake 3.30.4 from : $cmakeDownloaderPath"
                }
            }
            else {
                Write-Output "CMake download failed. Download the CMake file from : $cmakeUrl and install."
            }
        }
    }
}


Function download_gen_qnn_ctx_onnx {
    param()
    process {
        # Download generating qnn-ctx to onnx model python file 
        # Checking if already present 
        # If yes
        if (Test-Path $gen_qnn_ctx_onnx_FilePath ) {
            Write-Output "gen_qnn_ctx_onnx_model.py file already present at : $gen_qnn_ctx_onnx_FilePath " # -ForegroundColor Green
        }
        # Else downloading
        else {
            Write-Output "Downloading the gen_qnn_ctx_onnx_model.py ..." 
            $result = download_file -url $genQnnCtxOnnxUrl -downloadfile $gen_qnn_ctx_onnx_FilePath  
            # Checking for successful download
            if ($result) {
                Write-Output "File is downloaded at : $gen_qnn_ctx_onnx_FilePath" 

            } 
            else{
                Write-Output "gen_qnn_ctx_onnx_model.py download failed. Download the gen_qnn_ctx_onnx_model.py file from :  $genQnnCtxOnnxUrl" 
            }
        }
    }
}

Function download_onnxmodel {
    param()
    process {
        # Download Model file 
        # Checking if mobilenet.onnx already present 
        # If yes
        if (Test-Path $modelFilePath) {
            Write-Output "ONNX File already present at : $modelFilePath" # -ForegroundColor Green
        }
        # Else downloading
        else {
            Write-Output "Downloading the onnx model ..." 
            $result = download_file -url $modelUrl -downloadfile $modelFilePath
            # Checking for successful download
            if ($result) {
                Write-Output "Onnx File is downloaded at : $modelFilePath" 

            } 
            else{
                Write-Output "Onnx download failed. Download the onnx file from :  $modelUrl" 
            }
        }
    }
}

Function download_script_license{
    param()
    process{
        # License 
        # Checking if License already present 
        # If yes
        if(Test-Path $lincensePath){
            Write-Output "License is already downloaded at : $lincensePath"
        }
        # Else dowloading
        else{
            $result = download_file -url $licenseUrl -downloadfile $lincensePath
            if($result){
                Write-Output "License is downloaded at : $lincensePath"
            }
            else{
                Write-Output "License download failed. Download from $licenseUrl"
            }
        }
    }
}

Function download_mobilenet_artifacts{
    param ()
    process{
        # io_utils for pre and post processing for the mobilenet 
        # Checking if io_utils.py already present
        # If yes
        if(Test-Path $io_utilsPath){
            Write-Output "io_utils.py is already downloaded at : $io_utilsPath"
        }
        # Else dowloading
        else{
            $result = download_file -url $io_utilsUrl -downloadfile $io_utilsPath
            if($result){
                Write-Output "io_utils.py is downloaded at : $io_utilsPath"
            }
            else{
                Write-Output "io_utils.py download failed. Download from $io_utilsUrl"
            }
        }
    }
}

Function download_install_VS_Studio {
    param()
    process {
        # Checking if VStudio already installed
        # If yes
        if (Test-Path $vsInstallerPath) {
            Write-Output "VS-Studio already installed."
        }
        # Else downloading and installing VStudio
        else {
            Write-Output "Downloading the VS Studio..." 
            $result = download_file -url $vsStudioUrl -downloadfile $vsStudioDownloadPath
            # Checking for successful download
            if ($result) {
                Write-Output "VS Studio is downloaded at : $vsStudioDownloadPath" 
                Write-Output "installing VS-Studio..."
                if (install_VS_Studio) {
                    Write-Output "VS-Studio installed successfully." 
                }
                else{
                    Write-Output "VS-Studio installation failed..  from : $vsStudioDownloadPath"  
                }
            } 
            else{
                Write-Output "VS Studio download failed... Downloaded the VS Studio from :  $vsStudioUrl and install." 
            }
        }
    }
}


Function download_install_AI_Engine_Direct_SDK {
    param()
    process {
        
        # Checking if AI Engine Direct SDK already installed
        # If yes
	$SDK_Path = "$aIEngineSdkInstallPath\$QNN_SDK_Version"
        if (Test-Path $SDK_Path) {
            Write-Output "AI Engine Direct already present at :$SDK_Path" # -ForegroundColor Green
        }
        # Else downloading and installing AI Engine Direct SDK
        else {
            Write-Output "Downloading the AI Engine Direct..."
            $result = download_and_extract -artifactsUrl $aIEngineSdkUrl -rootDirPath $downloadDirPath
            # Checking for successful download
            if ($result) {
                Write-Output " AI Engine Direct Artifacts File is downloaded and extracted at : $downloadDirPath"
		$folderName = [System.IO.Path]::GetFileName($aIEngineSdkDownloadPath)
        	$destinationPath = Join-Path -Path $aIEngineSdkInstallPath -ChildPath $folderName
                if (-Not (Test-Path -Path $aIEngineSdkInstallPath)) {
                    New-Item -Path $aIEngineSdkInstallPath -ItemType Directory
                }
                if (-Not (Test-Path -Path $destinationPath)) {
                    Move-Item -Path $aIEngineSdkDownloadPath -Destination $destinationPath
                    Write-Output "AI Engine Direct installed successfully to $destinationPath"
                }
            }
            else{
                Write-Output "AI Engine Direct download failed... Downloaded the AI Engine Direct SDK from : $aIEngineSdkUrl and extract to $destinationPath " 
            }
        }
    }
}

Function mobilenet_artifacts{
    param ()
    process {
        download_onnxmodel
        download_mobilenet_artifacts
    }
}

Function gen_qnn_ctx_onnx{
    param()
    process {
        download_gen_qnn_ctx_onnx
    }

}

Function Check_Setup {
    param(
        [string]$logFilePath
    )
    process {
        $results = @()

        # Check if Python is installed
        if (Test-Path "$pythonInstallPath\python.exe") {
            $results += [PSCustomObject]@{
                Component = "Python"
                Status    = "Successful"
                Comments  = "$(python --version)"
            }
        } else {
            $results += [PSCustomObject]@{
                Component = "Python"
                Status    = "Failed"
                Comments  = "Download from $pythonUrl"
            }
        }

        # Check if Visual Studio is installed
        if (Test-Path $vsInstallerPath) {
            $results += [PSCustomObject]@{
                Component = "Microsoft Visual Studio"
                Status    = "Successful"
                Comments  = "Microsoft Visual Studio version 17.10.4"
            }
        } else {
            $results += [PSCustomObject]@{
                Component = "Microsoft Visual Studio"
                Status    = "Failed"
                Comments  = "Download from $vsStudioUrl"
            }
        }

        # Check if AI Engine SDKs is installed
        if (Test-Path "$aIEngineSdkInstallPath\$QNN_SDK_Version") {
            $results += [PSCustomObject]@{
                Component = "AI Engine SDK"
                Status    = "Successful"
                Comments  = "SDK version $QNN_SDK_Version"
            }
        } else {
            $results += [PSCustomObject]@{
                Component = "AI Engine SDK"
                Status    = "Failed"
                Comments  = "Download from $aIEngineSdkUrl"
            }
        }

        # Check if CMake is installed
        if (Test-Path "$cmakeInstallPath\bin\cmake.exe") {
            $results += [PSCustomObject]@{
                Component = "CMake"
                Status    = "Successful"
                Comments  = "$(cmake --version)"
            }
        } else {
            $results += [PSCustomObject]@{
                Component = "CMake"
                Status    = "Failed"
                Comments  = "Download from $cmakeUrl"
            }
        }

        # Output the results as a table
        $results | Format-Table -AutoSize

        # Store the results in a debug.log file
        $results | Out-File -FilePath $logFilePath
    }
}


############################## main code ##################################

Function QNN_Setup{
    param(
        [string]$rootDirPath = "C:\WoS_AI"
    )
    process{
        # Set the permission on PowerShell to execute the command. If prompted, accept and enter the desired input to provide execution permission.
        Set-ExecutionPolicy RemoteSigned
        Set_Variables -rootDirPath $rootDirPath
	download_file -url $pyscriptUrl -downloadfile $pyscriptDownloadPath
        download_install_python
        Show-Progress -percentComplete 1 6
        download_install_VS_Studio
        Show-Progress -percentComplete 2 6
        $SDX_QAIRT_VENV_Path = "$rootDirPath\$QAIRT_VENV_Path"
        if (-Not (Test-Path -Path $SDX_QAIRT_VENV_Path))
            {
                py -3.10 -m venv $SDX_QAIRT_VENV_Path
            }
        & "$SDX_QAIRT_VENV_Path\Scripts\Activate.ps1"
        pip install requests tqdm argparse
        download_install_AI_Engine_Direct_SDK
        Show-Progress -percentComplete 3 6
	deactivate
	download_install_cmake
        Show-Progress -percentComplete 4 6
        download_script_license
        mobilenet_artifacts
        Show-Progress -percentComplete 5 6
        
        if (Test-Path "$SDX_QAIRT_VENV_Path\Scripts\Activate.ps1") {
            & "$SDX_QAIRT_VENV_Path\Scripts\Activate.ps1" 
            # upgrade pip
            python -m pip install --upgrade pip
            #update the QNN version in the below command as needed. 
            & C:\Qualcomm\AIStack\QAIRT\$QNN_SDK_VERSION\bin\envsetup.ps1
            python "${QNN_SDK_ROOT}\bin\check-python-dependency"
            pip install psutil==6.0.0 
            pip install tensorflow==2.10.1 
            pip install tflite==2.3.0
            pip install torch==1.13.1
            pip install torchvision==0.14.1 
            pip install onnx==1.12.0
            pip install onnxruntime==1.17.1
            pip install onnxsim==0.4.36  
            pip install fiftyone
	    pip install requests
            pip install --upgrade opencv-python
            # checking all python dependency 
            python "${QNN_SDK_ROOT}\bin\check-python-dependency"
            # checking all winndow dependency
            & "${QNN_SDK_ROOT}\bin\check-windows-dependency.ps1"
            copy ${QNN_SDK_ROOT}\bin\aarch64-windows-msvc\qnn-net-run.exe ${qnndependenciesPath}
            copy ${QNN_SDK_ROOT}\lib\aarch64-windows-msvc\QnnHtp.dll ${qnndependenciesPath}
            copy ${QNN_SDK_ROOT}\lib\aarch64-windows-msvc\QnnHtpV73Stub.dll ${qnndependenciesPath}
            copy ${QNN_SDK_ROOT}\lib\aarch64-windows-msvc\QnnHtpPrepare.dll ${qnndependenciesPath}
            copy ${QNN_SDK_ROOT}\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so ${qnndependenciesPath}
            copy ${QNN_SDK_ROOT}\lib\hexagon-v73\unsigned\libqnnhtpv73.cat ${qnndependenciesPath}
        }
        gen_qnn_ctx_onnx
        Show-Progress -percentComplete 6 6
        Write-Output "***** Installation for AI Engine Direct(QNN)*****"
        Check_Setup -logFilePath "$debugFolder\QNN_Setup_Debug.log"
	    Invoke-Command { & "powershell.exe" } -NoNewScope
    }
}

Function Activate_QNN_VENV {
    param ( 
        [string]$rootDirPath = "C:\WoS_AI" 
    )
    process {
        $SDX_QAIRT_VENV_Path = "$rootDirPath\$QAIRT_VENV_Path"
        $global:DIR_PATH      = $rootDirPath
        cd "$DIR_PATH\$Mobilenet_Folder_path"
        & "$SDX_QAIRT_VENV_Path\Scripts\Activate.ps1"
        & C:\Qualcomm\AIStack\QAIRT\$QNN_SDK_VERSION\bin\envsetup.ps1
    }  
}
