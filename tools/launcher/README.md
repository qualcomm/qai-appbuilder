<br>

<div align="center">
  <h3>Setup the QAI AppBuilder runtime environment fast and easily.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## QAI AppBuilder Launcher 
This guide introduces how to setup the QAI AppBuilder runtime environment fast and easily through our Windows batch scripts.

## Usage
### 1. Download Windows batch scripts

There are two ways to get our Windows batch scripts:
1. Download the compressed package through [QAI_Launcher_v1.0.0.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAI_Launcher_v1.0.0.zip)  and reduce the compression; <br>
2. Download this git repository, you can find these scripts from the path 'ai-engine-direct-helper/tools/launcher'; <br>

### 2. Run these script one by one

|  Script   | Command  |
|  ----  | :----    |
| 1.Install_QAI_AppBuilder.bat | Setup QAI AppBuilder basic environment |
| 2.Install_LLM_Models.bat | Download and install [IBM-Granite-v3.1-8B-Instruct](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) LLM model automatically. You can replace this model with your LLM model. You can get detailed steps [here](../../samples/genie/python/README.md#setup-custom-model). |
| 3.Start_WebUI.bat | The launcher for 2 WebUI applications. |
| 4.Start_GenieAPIService.bat | Start [GenieAPIService](../../samples/genie/c++/). This is OpenAI Compatible API Service (C++ version).|
| 5.Install_LangFlow.bat | Install LangFlow conponents. You can get more information about LangFlow [here](../langflow/).|
| 6.Start_LangFlow.bat | Start LangFlow service. |
