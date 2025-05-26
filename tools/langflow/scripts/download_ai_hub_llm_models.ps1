#==============================================================================
#
#  Copyright (c) 2025 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
#==============================================================================

$userProfilePath = $env:USERPROFILE


function Move-FolderSafely {
    param (
        [string]$sourceFolder,
        [string]$destinationFolder
    )

    try {
        # 检查源文件夹是否存在
        if (-not (Test-Path -Path $sourceFolder -PathType Container)) {
            Write-Error "源文件夹 $sourceFolder 不存在。"
            return
        }

        # 检查目标路径是否存在同名文件或文件夹，若存在则删除
        if (Test-Path -Path $destinationFolder) {
            Remove-Item -Path $destinationFolder -Recurse -Force
        }

                
        # 检查目标文件夹的父目录是否存在，如果不存在则创建
        $destinationParent = Split-Path -Path $destinationFolder -Parent
        if (-not (Test-Path -Path $destinationParent -PathType Container)) {
            New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        }

        # 移动文件夹
        Move-Item -Path $sourceFolder -Destination $destinationFolder -Force -ErrorAction Stop
        Write-Host "文件夹 $sourceFolder 已成功移动到 $destinationFolder."
    }
    catch {
        Write-Error "移动文件夹时发生错误: $_"
    }
}

Function Download_ai_hub_llm_models {
    param()
    process {
        $ibm_grantite_url =  "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct_quantized/v1/snapdragon_x_elite/models.zip"
        $ibm_grantite_zip_path = 'ibm_granite_v3_1_8b_instruct_quantized.zip'

        $ibm_grantite_path = 'ibm_granite_v3_1_8b_instruct_quantized'

        $ibm_grantite_cache_path = "$userProfilePath\.cache\qualcomm\$ibm_grantite_zip_path"
        $ibm_grantite_destination_path = "$userProfilePath\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\IBM-Granite-v3.1-8B"

        if (Test-Path -Path $ibm_grantite_cache_path) {

        }
        else{

            Invoke-WebRequest -Uri $ibm_grantite_url -OutFile $ibm_grantite_cache_path
        }

        # Extract the zip file
        Expand-Archive -Path $ibm_grantite_cache_path -DestinationPath $ibm_grantite_destination_path -Force -ErrorAction Stop
        
        # Move the extracted folder to the destination path
        #Move-FolderSafely -sourceFolder $ibm_grantite_path -destinationFolder $ibm_grantite_destination_path

        



        # Download the tokenizer.json file
        #$ibm_grantite_tokenizer_url = "https://huggingface.co/ibm-granite/granite-3.1-8b-base/resolve/main/tokenizer.json"
        #Invoke-WebRequest -Uri $ibm_grantite_tokenizer_url -OutFile "$ibm_grantite_destination_path\tokenizer.json" -ErrorAction Stop





        if($false)
        {

            $phi_url = 'https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/phi_3_5_mini_instruct_quantized/v1/snapdragon_x_elite/models.zip'
            $phi_zip_path = 'phi_3_5_mini_instruct_quantized.zip'

            $phi_path = 'phi_3_5_mini_instruct_quantized'
            $phi_destination_path = "$userProfilePath\code\qualcomm\ai-engine-direct-helper\samples\genie\python\models\Phi-3.5-mini"
            $phi_cache_path = "$userProfilePath\.cache\qualcomm\$phi_zip_path"

            if(Test-Path -Path $phi_cache_path)
            {
            }
            else
            {

                Invoke-WebRequest -Uri $phi_url -OutFile $phi_cache_path 
        
            }


            # Extract the zip file
            Expand-Archive -Path $phi_cache_path -DestinationPath $phi_destination_path  -Force -ErrorAction Stop

            # Move the extracted folder to the destination path
            #Move-FolderSafely -sourceFolder $phi_path -destinationFolder $phi_destination_path

                        
            # Download the tokenizer.json file
            #$phi_tokenizer_url="https://huggingface.co/microsoft/Phi-3.5-mini-instruct/resolve/main/tokenizer.json"
            #Invoke-WebRequest -Uri $phi_tokenizer_url -OutFile "$phi_destination_path\tokenizer.json" -ErrorAction Stop    
    
        }

    }
}


