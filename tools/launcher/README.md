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
This guide introduces how to setup the QAI AppBuilder runtime environment fast and easily through our Windows batch scripts. <br>
Note: If the following automatic installation script doesn't work for some reason, please try to manually configure what you are interested in according to the following guidelines. <br>
[Setup Python Environment](../../docs/python.md) <br>
[WebUI Apps](../../samples/webui/) | [GenieAPIService (Python)](../../samples/genie/python/) | [GenieAPIService (C++)](../../samples/genie/c++/) | [Samples](../../samples/python/)

## Usage
### 1. Download Windows batch scripts

There are two ways to get our Windows batch scripts:
1. Download the compressed package through [QAI_Launcher_v1.0.0.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAI_Launcher_v1.0.0.zip)  and reduce the compression; <br>
2. Download this git repository, you can find these scripts from the path 'ai-engine-direct-helper/tools/launcher'; <br>

### 2. Run these script one by one

|  Script   | Description  |
|  ----  | :----    |
| 1.Install_QAI_AppBuilder.bat | Setup QAI AppBuilder basic environment. If this script has been executed before, executing it again will upgrade the content of the project. |
| 2.Install_LLM_Models.bat | Download and install [IBM-Granite-v3.1-8B-Instruct](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) LLM model automatically. <br>You can replace this model with your owner LLM model. Getting detailed steps [here](../../samples/genie/python/README.md#setup-custom-model) for using your custom model. |
| 3.Start_WebUI.bat | The launcher for starting 2 WebUI applications. If need the image generation feature, please run script '7.Start_StableDiffusion.bat' first. |
| 4.Start_GenieAPIService.bat | Start [GenieAPIService](../../samples/genie/c++/). This is OpenAI Compatible API Service (C++ version).|
| 5.Install_LangFlow.bat | Install LangFlow components. You can get more information about LangFlow [here](../langflow/). <br>When installing LangFlow, need to install Visual Studio. Visual Studio will be installed automatically during the installation process. If the Visual Studio Installer window pops up, please select 'Yes' to install it.|
| 6.Start_LangFlow.bat | Start LangFlow service (The startup speed is slow, so wait patiently. When it is ready, the browser will be started automatically.)<br> We also provided several flows for running QNN models on NPU. You can get detailed steps [here](https://github.com/quic/ai-engine-direct-helper/tree/main/tools/langflow#using-the-flows) for using these flows. |
| 7.Start_StableDiffusion.bat | Start Stable Diffusion WebUI. If model is not exist, it will download it automatically. |
| 8.Start_PythonEnv.bat | Start Python environment. We use [Pixi](https://pixi.sh/) to manage the Python, this script can setup the Python environment for you if you want to develop and run your owner Python script. |

## Possible problems and solutions:
### 1. Certificate issue
```
Pixi task (install-tools): python utils/Install_Tools.py
Error:   x Failed to update PyPI packages for environment 'default'
  |-> Failed to prepare distributions
  |-> Failed to download `jinja2==3.1.6`
  |-> Failed to fetch: `https://files.pythonhosted.org/packages/62/
  |   a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/jinja2-3.1.6-py3-none-any.whl`
  |-> Request failed after 3 retries
  |-> error sending request for url (https://files.pythonhosted.org/packages/62/
  |   a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/jinja2-3.1.6-py3-none-any.whl)
  |-> client error (Connect)
  `-> invalid peer certificate: UnknownIssuer
```

#### Solution: <br>
Create a new file and save the following content to file 'C:\\Users\\< user name >\\.pixi\\config.toml'
```
tls-no-verify = true
[pypi-config]
allow-insecure-host = ["*"]
```

### 2. Network timeout issue
```
Pixi task (install-tools): python utils/Install_Tools.py
Error:   x Failed to update PyPI packages for environment 'default'
  |-> Failed to prepare distributions
  |-> Failed to download `jinja2==3.1.6`
  |-> Failed to fetch: `https://files.pythonhosted.org/packages/62/
  |   a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/jinja2-3.1.6-py3-none-any.whl`
  |-> Request failed after 3 retries
  |-> error sending request for url (https://files.pythonhosted.org/packages/62/
  |   a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/jinja2-3.1.6-py3-none-any.whl)
  `-> operation timed out
```

#### Solution: <br>
It's network issue, perhaps you need to use proxy. In "Command Prompt" window, you can use the following commands to set proxy:
```
set HTTP_PROXY=http://<proxy ip address>:<port>
set HTTPS_PROXY=http://<proxy ip address>:<port>
```
