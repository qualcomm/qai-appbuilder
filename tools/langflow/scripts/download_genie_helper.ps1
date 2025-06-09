# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

Function Download_Genie_Helper {
    param()
    process {

        $userProfilePath = $env:USERPROFILE


        $qai_libs_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAIRT_LIBS_2.34_v73.zip"
        $qai_appbuilder_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl"
        $wget_url = "https://eternallybored.org/misc/wget/releases/wget-1.21.4-winarm64.zip"
        $qai_libs_zip = Split-Path -Leaf $qai_libs_url
        $qai_appbuilder_whl = Split-Path -Leaf $qai_appbuilder_url
        $qai_libs_cache_path = "$userProfilePath\.cache\qualcomm\$qai_libs_zip"
        $qai_appbuilder_cache_path = "$userProfilePath\.cache\qualcomm\$qai_appbuilder_whl"

        if(Test-Path -Path $qai_libs_cache_path){
            Write-Output "qai_libs is ready..."
        }
        else{
            Write-Output "download qai_libs..."
            Invoke-WebRequest -Uri $qai_libs_url -OutFile $qai_libs_cache_path
        }

        if(Test-Path -Path $qai_appbuilder_cache_path){
            Write-Output "qai appbuilder whl is ready..."
        }
        else{
            Write-Output "download appbuilder whl..."
            Invoke-WebRequest -Uri $qai_appbuilder_url -OutFile $qai_appbuilder_cache_path
        }

        $destPath = Join-Path -Path $userProfilePath -ChildPath "code\qualcomm"
        $destQaiLibsPath = "$destPath\arm64x-windows-msvc"
        New-Item -ItemType Directory -Path $destPath -Force

        Expand-Archive -Path $qai_libs_cache_path -DestinationPath $destPath -Force -ErrorAction Stop 
        Copy-Item -Path "$qai_appbuilder_cache_path" -Destination $destPath -Force -ErrorAction Stop

        $sourceQaiAppbuilderPath = "$PSScriptRoot\..\..\..\samples"
        $destQaiAppbuilderPath = Join-Path -Path $destPath -ChildPath "ai-engine-direct-helper\samples"
        $targetQaiLibPath = Join-Path -Path $destQaiAppbuilderPath -ChildPath "qai_libs"
        New-Item -ItemType Directory -Path $destQaiAppbuilderPath -Force

        Copy-Item -Path "$sourceQaiAppbuilderPath\*" -Destination $destQaiAppbuilderPath -Recurse -Force -ErrorAction Stop
        if(Test-Path -Path $targetQaiLibPath){
            Copy-Item -Path "$destQaiLibsPath\*" -Destination $targetQaiLibPath -Force -ErrorAction Stop
        }
        else{
            New-Item -ItemType Directory -Path $targetQaiLibPath -Force
            Copy-Item -Path "$destQaiLibsPath\*" -Destination $targetQaiLibPath -Force -ErrorAction Stop
        }
        $tool_path = "$destPath\ai-engine-direct-helper\samples\tools"
        $wget_path = "$destPath\ai-engine-direct-helper\samples\tools\wget"
        $wget_zip_path = "$tool_path\wget.zip"
        New-Item -ItemType Directory -Path $tool_path -Force
        New-Item -ItemType Directory -Path $wget_path -Force
        Invoke-WebRequest -Uri $wget_url -OutFile $wget_zip_path
        Expand-Archive -Path $wget_zip_path -DestinationPath $wget_path -Force -ErrorAction Stop

        $phiTokenFile = "$PSScriptRoot\..\..\..\tools\langflow\config\phi_3_5_mini_instruct_quantized\tokenizer.json"
        $ibmTokenFile = "$PSScriptRoot\..\..\..\tools\langflow\config\ibm_granite_v3_1_8b_instruct_quantized\tokenizer.json"
        Invoke-WebRequest -Uri "https://huggingface.co/microsoft/Phi-3.5-mini-instruct/resolve/main/tokenizer.json?download=true" -OutFile $phiTokenFile -ErrorAction Stop
        Invoke-WebRequest -Uri "https://huggingface.co/ibm-granite/granite-3.1-8b-base/resolve/main/tokenizer.json?download=true" -OutFile $ibmTokenFile -ErrorAction Stop
    }
}

