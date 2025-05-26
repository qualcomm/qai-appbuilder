#Set it to print debug information
Set-PSDebug -Trace 0

#Get the environment 
$userProfilePath = $env:USERPROFILE
$scriptPath = $PSScriptRoot


& ".\stop.ps1"


#download and install visual studio
. ".\install_visual_studio.ps1"
download_install_VS_Studio


#install python

. ".\install_python.ps1" 
download_install_python



. ".\utils.ps1" 

#Delete the code\myenvs folder
$myenvsPath = Join-Path -Path $userProfilePath -ChildPath "code\myenvs"

Write-Output " create env $myenvsPath"

$langflow_py312 = "$myenvsPath\langflow-new-py3.12"

if (Utils_Delete_Folder_With_Retry -folderPath $langflow_py312) {
    if ($env:USERNAME -eq "xiaoshi") {
        #Write-Host "The current user is xiaoshi."
    } else {
        Utils_Delete_Folder_With_Retry -folderPath "$userProfilePath\code\github" 
    }
    Write-Output "env folders deleted"
}    



#Create venv for langflow-py3.12
New-Item -ItemType Directory -Path $myenvsPath -Force

#Create .cache\qualcomm\
$cache_dir = Join-Path -Path $userProfilePath -ChildPath ".cache\qualcomm\"
New-Item -ItemType Directory -Path $cache_dir -Force
$python312=(Get-Command python).source
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


Utils_Delete_Folder_With_Retry -folderPath "$userProfilePath\code\qualcomm" | Out-Null


. ".\utils.ps1"
. ".\download_genie_helper.ps1"
Download_Genie_Helper

if($false)
{
    # Source folder path (network share)
    $sourcePath = "\\harbor\chinace\WoS\AI_Apps\GenieWebUI"


    $cache_dir="$userProfilePath\.cache\qualcomm\GenieWebUI"

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

    $destinationPath = "$userProfilePath\code\qualcomm\GenieWebUI"

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

uv pip install "$userProfilePath\code\qualcomm\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl"
deactivate


#Copy the script folder .

New-Item -ItemType Directory -Path "$userProfilePath\code\qualcomm\langflow_cv\install_scripts" -Force

Copy-Item -Path ".\start_langflow.ps1" -Destination "$userProfilePath\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\start_langflow_only.ps1" -Destination "$userProfilePath\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\stop.ps1" -Destination "$userProfilePath\code\qualcomm\langflow_cv\install_scripts\."
Copy-Item -Path ".\start_langflow.bat" -Destination "$userProfilePath\code\qualcomm\langflow_cv\install_scripts\."


#Copy the flows to folder.
New-Item -ItemType Directory -Path "$userProfilePath\Downloads\flows" -Force
Copy-Item -Path "..\flows\*" -Destination "$userProfilePath\Downloads\flows\."



#Delete the code\myenvs folder
$myenvsPath = Join-Path -Path $userProfilePath -ChildPath "code\myenvs"


$clean_py312 = "$myenvsPath\clean-py3.12"

if (Utils_Delete_Folder_With_Retry -folderPath $clean_py312) {
    if ($env:USERNAME -eq "xiaoshi") {
        #Write-Host "The current user is xiaoshi."
    } else {
        Utils_Delete_Folder_With_Retry -folderPath "$userProfilePath\code\github" 
    }
    Write-Output "env folders deleted"
}    




$python312=(Get-Command python).source
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

pip install $userProfilePath\code\qualcomm\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl --force-reinstall


deactivate

#Install ollama
& ".\install_ollama.ps1"



# Get the path of the desktop
$desktopPath = [System.Environment]::GetFolderPath("Desktop")

# Define the path and name of the shortcut
$shortcutPath = Join-Path -Path $desktopPath -ChildPath "start_langflow.lnk"

# Define the target path of the shortcut
$targetPath = "$userProfilePath\code\qualcomm\langflow_cv\install_scripts\start_langflow.bat"

# Create a WScript.Shell object
$wshShell = New-Object -ComObject WScript.Shell

# Create the shortcut
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath

# Set the working directory to the original folder of the batch file
$shortcut.WorkingDirectory = Split-Path -Path $targetPath

$shortcut.Save()






#Download github llm models
. ".\download_ai_hub_llm_models.ps1"
Download_ai_hub_llm_models
#Copy the config and prompt files
if($false)
{
    Copy-Item -Path "..\config\phi_3_5_mini_instruct_quantized\tokenizer.json" -Destination "$userProfilePath\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\Phi-3.5-mini\."

}
Copy-Item -Path "..\config\ibm_granite_v3_1_8b_instruct_quantized\tokenizer.json" -Destination "$userProfilePath\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\IBM-Granite-v3.1-8B\."


#Since ai direct helper need python3.10 and can't work from the git hub. Hold it.
if($false)
{

    . ".\download_ai_engine_direct_helper.ps1"
    Download_ai_helper

}



# #Add the patch for the ServiceAPI.py
# # 定义文件路径
# $filePath = "GenieAPIService.py"

# # 读取文件内容为数组，每一行是一个元素
# $lines = Get-Content -Path $filePath

# # 定义要添加的注释
# $comment = "# Add comments"

# # 检查是否有足够的行数
# if ($lines.Count -ge 194) {
#     # 在第 194 行添加注释
#     $lines[193] = "$($lines[193]) $comment"
#     # 将修改后的内容写回文件
#     $lines | Set-Content -Path $filePath
#     Write-Host "Already add comment on Line 194 "
# } else {
#     Write-Host "Can not find line 94"
# }    


# Wait for any key input to end
Write-Host "Install successfully. Press any key to exit..."
[void][System.Console]::ReadKey($true)






