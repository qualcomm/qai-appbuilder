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
[WebUI Apps](../../samples/webui/) | [GenieAPIService (Python)](../../samples/genie/python/) | [GenieAPIService (C++)](../../samples/genie/c++/) | [Samples](../../samples/)

## Usage
### 1. Download Windows batch scripts

There are two ways to get our Windows batch scripts:
1. Download the compressed package through [QAI_Launcher_v2.0.0.zip](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.38.0/QAI_Launcher_v2.0.0.zip) and extract it; <br>
2. Download this git repository, you can find these scripts from the path `qai-appbuilder/tools/launcher`; <br>

### 2. Run these scripts one by one

|  Script   | Description  |
|  ----  | :----    |
| `1.Install_QAI_AppBuilder.bat` | **[Run first]** Sets up the complete QAI AppBuilder environment. Installs [aria2c](https://github.com/minnyres/aria2-windows-arm64/releases) (ARM64 multi-threaded downloader) and [Pixi](https://pixi.sh/) (v0.49.0, ARM64) if not present, configures Pixi TLS settings, then runs `Install_Tools.py` to install wget, Git (ARM64 portable), Visual C++ Redistributable, clone/update the `qai-appbuilder` repository, and download [GenieAPIService](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.44.0/GenieAPIService_Stable_QAIRT_v73.zip). Finally installs the QAI AppBuilder Python package. Re-running this script will pull the latest changes from the repository. |
| `2.Install_LLM_Models.bat` | Downloads and installs the [IBM-Granite-v3.1-8B-Instruct](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) LLM model (serialized `.bin` files) and its tokenizer automatically into `qai-appbuilder/samples/genie/python/models/IBM-Granite-v3.1-8B/`. <br>You can replace this model with your own LLM model. See [here](../../samples/genie/python/README.md#setup-custom-model) for steps on using a custom model. |
| `3.Start_WebUI.bat` | Launches one of the following WebUI applications (interactive selection): <br>**1. ImageRepairApp** — AI-powered image inpainting/repair WebUI. <br>**2. GenieWebUI** — LLM chat WebUI powered by Genie. |
| `4.Start_GenieAPIService.bat` | Starts the [GenieAPIService](../../samples/genie/c++/) — an OpenAI-compatible C++ API service. Defaults to `IBM-Granite-v3.1-8B` model config. Supports passing a custom model config name as a parameter, e.g.: `4.Start_GenieAPIService.bat "Qwen2.0-7B-SSD"`. Keep this window open while the service is running. |
| `5.Start_StableDiffusion.bat` | Starts the Stable Diffusion v3.5 image generation WebUI (`StableDiffusionApp.py`). If the model files are not present, they will be downloaded automatically. Automatically sets `HF_ENDPOINT` (HuggingFace mirror) and `HF_HOME` (local cache path) to avoid common download and path-length issues. |
| `6.Start_PythonEnv.bat` | Activates the Pixi-managed Python 3.12 environment shell. Use this to run your own Python scripts or develop within the pre-configured environment (includes torch, transformers, diffusers, gradio, etc.). |

### 3. Directory structure

```
tools/launcher/
├── 1.Install_QAI_AppBuilder.bat   # Main setup script
├── 2.Install_LLM_Models.bat       # LLM model downloader
├── 3.Start_WebUI.bat              # WebUI launcher (ImageRepair / GenieWebUI)
├── 4.Start_GenieAPIService.bat    # C++ Genie API service launcher
├── 5.Start_StableDiffusion.bat    # Stable Diffusion v3.5 WebUI launcher
├── 6.Start_PythonEnv.bat          # Python environment shell
├── env/
│   └── pixi.toml                  # Pixi workspace config (Python 3.12, all dependencies)
└── utils/                         # Internal helper scripts (called by .bat scripts)
    ├── Install_Helper.py          # Download utilities (aria2c / wget / requests fallback)
    ├── Install_LLM_Models.py      # LLM model download & extraction logic
    ├── Install_Pixi.ps1           # Pixi installer & environment setup
    ├── Install_QAI_AppBuilder.ps1 # QAI AppBuilder package installer
    ├── Install_Tools.py           # Installs wget, Git, VC++ Redist, clones repo, downloads GenieAPIService
    ├── Install_Visual_Studio.ps1  # Visual Studio Build Tools checker & installer
    ├── Install_Visual_Studio.py   # VS Build Tools download & silent install
    ├── Set_Pixi_Config.ps1        # Writes Pixi TLS config to ~/.pixi/config.toml
    └── Start_WebUI.ps1            # Interactive WebUI selector (ImageRepair / GenieWebUI)
```

### 4. Python environment dependencies (env/pixi.toml)

The Pixi environment (`py312`) provides Python 3.12 with the following key packages:

| Category | Packages |
| ---- | :---- |
| Deep Learning | `torch`, `torchvision`, `torchaudio` |
| AI / Models | `transformers==4.45`, `diffusers>=0.34`, `ultralytics==8.0.193` |
| QAI Hub | `qai-hub==0.30.0`, `qai-hub-models==0.30.2` |
| WebUI | `gradio==5.35.0` |
| API Service | `fastapi`, `uvicorn`, `pydantic-settings`, `sse-starlette` |
| LangChain | `langchain`, `langchain-core`, `langchain-community` |
| Utilities | `numpy`, `pillow`, `opencv-python`, `huggingface-hub`, `openai`, `pypdf`, `python-pptx`, `docx2txt` |

## Possible problems and solutions
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

#### Solution:
`1.Install_QAI_AppBuilder.bat` automatically calls `Set_Pixi_Config.ps1` to write the following config. If the issue persists, manually create the file `C:\Users\<username>\.pixi\config.toml` with the content below:
```toml
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

#### Solution:
It's a network issue; you may need to use a proxy. In a **Command Prompt** window, set the proxy before running the scripts:
```bat
set HTTP_PROXY=http://<proxy ip address>:<port>
set HTTPS_PROXY=http://<proxy ip address>:<port>
```

### 3. Stable Diffusion tokenizer download fails (HuggingFace access issue)

`5.Start_StableDiffusion.bat` automatically sets `HF_ENDPOINT=https://hf-api.gitee.com` as a mirror. If the tokenizer still fails to download, you can override it manually before running:
```bat
set HF_ENDPOINT=https://hf-mirror.com
```

### 4. Windows path length limit (260 characters)

`5.Start_StableDiffusion.bat` automatically sets `HF_HOME=%TEMP%\hf_cache` to keep the HuggingFace cache path short. If you encounter path-too-long errors in other contexts, enable long path support in Windows:
```
Settings → System → For Developers → Enable Win32 long paths
```
