# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# Get current PowerShell version
$version = $PSVersionTable.PSVersion

# Check if the major version is less than 7
if ($version.Major -lt 7) {
    Write-Host "Current PowerShell version $version does not meet the requirement." -ForegroundColor Red
    Write-Host "Please install PowerShell 7 according to the README document and try again." -ForegroundColor Red
    exit 1
} else {
    Write-Host "PowerShell version meets the requirement: $version" -ForegroundColor Green
    Write-Host "Starting Langflow Setup ..." -ForegroundColor Green
}    

# Check the administrator's permissions
function Test-Administrator {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $user
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# Check and output the result
if (Test-Administrator) {
    Write-Host "The current PowerShell terminal window has administrator privileges and starts to execute..." -ForegroundColor Green
} else {
    Write-Host "The current PowerShell terminal window does not have administrator privileges. Please reopen the Powershell terminal window as an administrator and try again." -ForegroundColor Red
    exit 1
}

#Set it to print debug information
Set-PSDebug -Trace 0

#Get the environment 
$userProfilePath = $env:USERPROFILE
$scriptPath = $PSScriptRoot

#Create cache
$cache_dir = "..\cache\qualcomm\"
$cache_model_phi = "..\cache\phi_3_5_mini_instruct_quantized\"
$cache_model_ibm = "..\cache\ibm_granite_v3_1_8b_instruct_quantized\"
$cache_download = "..\cache\download\"
New-Item -ItemType Directory -Path $cache_dir -Force
New-Item -ItemType Directory -Path $cache_model_phi -Force
New-Item -ItemType Directory -Path $cache_model_ibm -Force
New-Item -ItemType Directory -Path $cache_download -Force

& ".\stop.ps1"


#download and install visual studio
. ".\install_visual_studio.ps1"
download_install_VS_Studio


#install python
. ".\install_python.ps1" 
download_install_python


# Check python exe files
$pythonPaths = @()
# Check python in PATH
$env:PATH.Split(';') | ForEach-Object {
    $path = $_
    if ([string]::IsNullOrEmpty($path)) { return }
    
    if (Test-Path (Join-Path $path "python.exe")) {
        $pythonExe = Join-Path $path "python.exe"
        try {
            $version = & $pythonExe --version 2>&1
            if ($version -match "Python (\d+\.\d+\.\d+)") {
                $pythonPaths += [PSCustomObject]@{
                    Version = $Matches[1]
                    Path = $pythonExe
                }
            }
        } catch {
            Write-Warning " Cannot be obtained $pythonExe version information"
        }
    }
}
# show all of the python version
if ($pythonPaths.Count -eq 0) {
    Write-Error "No installed version of Python was found"
    exit 1
}
Write-Host "Find the following Python versions:" -ForegroundColor Cyan
$pythonPaths | Format-Table Version, Path
# Find Python 3.12.x
$python312_info = $pythonPaths | Where-Object { $_.Version -match "^3\.12\." } | Sort-Object { [Version]$_.Version } -Descending | Select-Object -First 1

if (-not $python312_info) {
    Write-Error "Version 3.12.x of Python was not found"
    exit 1
}
Write-Host "Find Python version: $($python312_info.Version), Path:$($python312_info.Path)" -ForegroundColor Green


. ".\utils.ps1" 

#Delete the code\myenvs folder
$myenvsPath = "..\code\myenvs"

Write-Output " create env $myenvsPath"

$langflow_py312 = "$myenvsPath\langflow-new-py3.12"

if (Utils_Delete_Folder_With_Retry -folderPath $langflow_py312) {
    if ($env:USERNAME -eq "xiaoshi") {
        #Write-Host "The current user is xiaoshi."
    } else {
        Utils_Delete_Folder_With_Retry -folderPath "..\code\github" 
    }
    Write-Output "env folders deleted"
}    



#Create venv for langflow-py3.12
New-Item -ItemType Directory -Path $myenvsPath -Force

$python312=(Get-Command $python312_info.Path).source
Start-Process -FilePath  $python312 -ArgumentList "-m venv $langflow_py312" -Wait -WindowStyle Hidden

#Activate the venv
& "$langflow_py312\Scripts\Activate.ps1"

python -m pip install --upgrade pip
pip install requests wget tqdm importlib-metadata qai-hub qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers ultralytics==8.0.193
#install uv and langflow 
pip install uv 
# add native-tls
uv pip install --upgrade pip --native-tls
uv pip install langflow==1.1.1 --native-tls
uv pip install --upgrade pip --native-tls
uv pip install opencv-python torch torchvision wget scipy --native-tls


Utils_Delete_Folder_With_Retry -folderPath "..\code\qualcomm" | Out-Null


. ".\utils.ps1"
. ".\download_genie_helper.ps1"
Download_Genie_Helper

if($false)
{
    # Source folder path (network share)
    $sourcePath = "\\harbor\chinace\WoS\AI_Apps\GenieWebUI"


    $cache_dir="..\cache\qualcomm\GenieWebUI"

    if(Test-Path $cache_dir){

    }
    else {


        $executionTime = Measure-Command {
        # Call the function to copy files with progress
        Utils_Copy_WithProgress -source $sourcePath -destination $cache_dir -filterpattern "model"
        }


        Write-Host "Utils_Copy_WithProgress cost: $($executionTime.TotalSeconds) seconds"
        Write-Host "Copy operation completed."
        

    }

    $destinationPath = "..\code\qualcomm\GenieWebUI"

    $executionTime = Measure-Command {
        # Call the function to copy files with progress
        Write-Host $cache_dir
        Write-Host $destinationPath
        #Utils_Copy_WithProgress -source $cache_dir -destination $destinationPath -filterpattern "model"

        Copy-Item -Path $cache_dir -destination $destinationPath -Recurse -Force -ErrorAction Stop
    }
    
    Write-Host "Copy-WithProgress cost: $($executionTime.TotalSeconds) seconds"
    Write-Host "Copy operation completed."

    
}

uv pip install "..\code\qualcomm\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl"
deactivate


#Copy the script folder .

New-Item -ItemType Directory -Path "..\code\qualcomm\langflow_cv\install_scripts" -Force

Copy-Item -Path ".\start_langflow.ps1" -Destination "..\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\start_langflow_only.ps1" -Destination "..\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\stop.ps1" -Destination "..\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\start_langflow.bat" -Destination "..\code\qualcomm\langflow_cv\install_scripts\."


#Delete the code\myenvs folder
$myenvsPath = "..\code\myenvs"


$clean_py312 = "$myenvsPath\clean-py3.12"

if (Utils_Delete_Folder_With_Retry -folderPath $clean_py312) {
    if ($env:USERNAME -eq "xiaoshi") {
        #Write-Host "The current user is xiaoshi."
    } else {
        Utils_Delete_Folder_With_Retry -folderPath "..\code\github" 
    }
    Write-Output "env folders deleted"
}    




$python312=(Get-Command $python312_info.Path).source
Start-Process -FilePath  $python312 -ArgumentList "-m venv $clean_py312" -Wait -WindowStyle Hidden




##Install qai-apibuilder in host python
write-Output "Start to install ai-healper"

##Install qai-apibuilder in host python
write-Output "Start to use the $clean_py312"

#Activate the venv
& "$clean_py312\Scripts\Activate.ps1"

python -m pip install --upgrade pip
pip install requests wget tqdm importlib-metadata qai-hub qai_hub_models huggingface_hub Pillow numpy opencv-python torch torchvision torchaudio transformers diffusers ultralytics==8.0.193
pip install uvicorn pydantic_settings fastapi langchain langchain_core langchain_community sse_starlette pypdf python-pptx docx2txt openai opencv_python

pip install "..\code\qualcomm\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl" --force-reinstall


deactivate

#Install ollama
& ".\install_ollama.ps1"



# Get the path of the desktop
$desktopPath = [System.Environment]::GetFolderPath("Desktop")

# Define the path and name of the shortcut
$shortcutPath = Join-Path -Path $desktopPath -ChildPath "start_langflow.lnk"

# Define the target path of the shortcut
$targetPath = "..\code\qualcomm\langflow_cv\install_scripts\start_langflow.bat"

# Create a WScript.Shell object
$wshShell = New-Object -ComObject WScript.Shell

# Create the shortcut
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath

# Set the working directory to the original folder of the batch file
$shortcut.WorkingDirectory = Split-Path -Path $targetPath

$shortcut.Save()


#Download github llm models
Write-Host "Start download ai hub llm model"
. ".\download_ai_hub_llm_models.ps1"
Download_ai_hub_llm_models
Write-Host "Downloading model completed."
#Copy the config and prompt files
if($false)
{
    Copy-Item -Path "$cache_model_phi\tokenizer.json" -Destination "..\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\Phi-3.5-mini\."

}
Copy-Item -Path "$cache_model_ibm\tokenizer.json" -Destination "..\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\IBM-Granite-v3.1-8B\."


#Since ai direct helper need python3.10 and can't work from the git hub. Hold it.
if($false)
{

    . ".\download_ai_engine_direct_helper.ps1"
    Download_ai_helper

}



Write-Host "Install successfully. Press any key to exit..." -ForegroundColor Green
[void][System.Console]::ReadKey($true)






