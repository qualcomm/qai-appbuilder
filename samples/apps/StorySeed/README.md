<br>

<div align="center">
  <h3>AI Application to Generate English Story And Post to Xiaohongshu Automatically</h3>
</div>

<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This application randomly selects four words from a list of 200 elementary-level English words, uses generative AI to create a story and image, and automatically publishes the results to Xiaohongshu.

## Setting Up Environment
### Step 1: Install Dependencies
• Please refer to [Run the large language model on the local NPU](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/python/README.md).

• Please download [GenieAPIService](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.38.0/GenieAPIService_v2.1.0_QAIRT_v2.38.0_v73.zip) and copy the files in it into the directory "GenieAPIService" under the path "ai-engine-direct-helper\samples".

• Please download [Qwen2.0-7B-SSD Model](https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd.zip). Extract it and copy the folder 'Qwen2.0-7B-SSD' into the directory 'ai-engine-direct-helper\samples\genie\python\models'.

• Run the following 2 commands to launch the Service：
```
cd ai-engine-direct-helper\samples
GenieAPIService\GenieAPIService.exe -c "genie\python\models\Qwen2.0-7B-SSD\config.json"  -l
```
If the service prints the following logs, indicating that GenieAPIService started successfully.
```
config_file: genie\python\models\Qwen2.0-7B-SSD\config.json
model_name:
load model: Yes

model path: genie\python\models\Qwen2.0-7B-SSD
model name: Qwen2.0-7B-SSD
INFO: loading model <<< Qwen2.0-7B-SSD >>>
[INFO]  "Using create From Binary"
[INFO]  "Allocated total size = 161120768 across 10 buffers"
INFO: model <<< Qwen2.0-7B-SSD >>> is ready!
INFO: [TIME] | model load time >> 2772.02 ms
INFO: Service Is Ready Now!
```
**Note: Once the model works normally, press 'Ctrl + C' to stop GenieAPIService to avoid affect the execution of this app. Later at step3 after run StorySeed.py GenieAPIService will be started automatically, please do not stop it when running.**

### Step 2: Install Required Tools

• Download and install node.js from [Nodejs Download](https://nodejs.org/zh-cn/download).

• Download and install Chrome Browser from [Chrome Download](https://www.google.cn/intl/zh-CN/chrome/next-steps.html?statcb=1&installdataindex=empty&defaultbrowser=0).

After Chrome is installed, get its version in PowerShell:

```powershell
(Get-Item "C:\Program Files\Google\Chrome\Application\chrome.exe").VersionInfo
```

• Install the chromedriver which is same as installed chrome version got in last step (take version *137.0.7151.56* as example):

```powershell
npm config set strict-ssl false
npx @puppeteer/browsers install chromedriver@137.0.7151.56
```

Note: please make sure fill in the actual chrome version to install chromedriver.

### Step 3: Run the Application

• Enter the directory and execute the python script:

```
cd ai-engine-direct-helper\samples\
python apps\StorySeed\StorySeed.py
```

• Follow the command prompts to enter your phone number and the repeat publishing interval. 
```
[Launch] Opening GenieAPIService in new window with model 'Qwen2.0-7B-SSD'...
Genie Service Started
⚠️Please follow the process in Command Line. Don't do any operations in Chrome Browser⚠️
⚠️Please follow the process in Command Line. Don't do any operations in Chrome Browser⚠️
⚠️Please follow the process in Command Line. Don't do any operations in Chrome Browser⚠️
 Please input your correct phone number:  13912345678
 Please input the period (in minutes) to post red (Recommended value > 2 minutes): 60
```
• To login at the first time, you have to **input verification code in command line,
and DO NOT input phone number or verification code on the Chrome browser.**
```
⚠️Please input the verification code in Command line, NOT in Browser⚠️
Try to load cookies from json: C:\codes\ai-engine-direct-helper\samples\apps\StorySeed\red_cookies.json
WARNING: All log messages before absl::InitializeLog() is called are written to STDERR
I0000 00:00:1753436926.115219   12300 voice_transcription.cc:58] Registering VoiceTranscriptionCapability
Clean all the inactive cookies
Login Manually
Send verification code
Get verification code
 ⚠️Please enter the correct verification code: 123456
```
• Just wait a few moment, the script will automatically publish English short story to Xiaohongshu.

After the story is posted, you can see the update in [Xiaohongshu](https://creator.xiaohongshu.com/new/note-manager).

### Note
• If you want to use another mobile phone number to login, please delete existing file red_cookies.json firstly.
• Don't start any other GenieAPIService when executing this app.
• If you want to use another AI model but not 'Qwen2.0-7B-SSD', for example IBM-Granite-v3.1-8B model, please modify the variable MODEL_NAME in StorySeed.py to be "IBM-Granite-v3.1-8B" , and please copy the model files into the directory 'ai-engine-direct-helper\samples\genie\python\models', try below commands to ensure GenieAPIService can be started successfully(remember press Ctrl+C to stop it manually).
```
GenieAPIService\GenieAPIService.exe -c "genie\python\models\IBM-Granite-v3.1-8B\config.json"  -l
```