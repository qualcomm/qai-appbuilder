# Python(x64)

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This guide helps developers setup Python environment for using QAI AppBuilder on Windows on Snapdragon (WoS) platforms. <br>
You can also run the batch file from [QAI AppBuilder Launcher](../tools/launcher/) to setup the environment automatically.

## Setting Up QAI AppBuilder Python Environment:

### Step 1: Install Dependencies
Download and install [git](https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe) and [X64 Python 3.12.8](https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe) <br>

<a href="https://github.com/quic/ai-engine-direct-helper/blob/main/docs/python.md#step-1-install-dependencies"><img src="https://img.shields.io/badge/Note: - Windows on Snapdragon (WoS) device support running X86, X64, ARM64EC and ARM64 applications.-important"></a> <br>
*Note: For ease of use, we recommend using the X64 version of Python to run our sample applications. That’s why we’ve provided a download link for the X64 version of Python above.* <br>
*Note: Make sure to check 'Add python.exe to PATH' while install Python* <br>
*Note: If there are multiple Python versions in your environment, make sure that the Python installed this time is in the first place in the PATH environment variable.* <br>
To check the list of Python versions in your PATH environment variable, open a 'Command Prompt' window (not PowerShell), and run the command 'where python' as shown below: <br>
```
where python
C:\Programs\Python\Python312\python.exe
C:\Users\zhanweiw\AppData\Local\Microsoft\WindowsApps\python.exe
```

Download and install [vc_redist.x64](https://aka.ms/vs/17/release/vc_redist.x64.exe) if you haven't installed.

### Step 2: Install basic Python dependencies:
Run below command in Windows terminal:
```
pip install requests py3-wget tqdm importlib-metadata qai-hub 
```

### Step 3: Download QAI AppBuilder repository:
Run below command in Windows terminal:
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```
If you have cloned it before, you can update the code by the following command:
```
cd ai-engine-direct-helper
git pull --recurse-submodules
```
### Step 4: Setup QAI AppBuilder Python Environment:
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
python python\setup.py
```

### Now you can try all the functions we provided. We suggest you try the [WebUI Applications](../samples/webui/README.md) first.

