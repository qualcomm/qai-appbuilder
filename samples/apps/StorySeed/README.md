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
Refer to [Run the large language model on the local NPU](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/python/README.md) to make sure IBM-Granite-v3.1-8B model can run normally.

Run below commands in Windows termial to install more python dependencies:
```
pip install selenium==4.34.2
```
**Note: Once the model works normally, stop the model and Genie API Service to avoid affect the execution of this app. This application will start them automatically when running.**

### Step 2: Install Required Tools

Download and install node.js from [Nodejs Download](https://nodejs.org/zh-cn/download).

Download and install Chrome Browser from [Chrome Download](https://www.google.cn/intl/zh-CN/chrome/next-steps.html?statcb=1&installdataindex=empty&defaultbrowser=0).

After Chrome is installed, get its version in PowerShell:

```powershell
(Get-Item "C:\Program Files\Google\Chrome\Application\chrome.exe").VersionInfo
```

Install the chromedriver which is same as installed chrome version got in last step (take version *137.0.7151.56* as example):

```powershell
npm config set strict-ssl false
npx @puppeteer/browsers install chromedriver@137.0.7151.56
```

**Note: make sure fill in the actual chrome version to install chromedriver.** 

### Step 3: Run the Application

Enter the directory:

```
cd ai-engine-direct-helper\samples\
```

Execute the script:

```
python apps\StorySeed\StorySeed.py
```

Follow the prompts to enter your phone number and verification code (required for the first time).

Enter the automatic publishing cycle in seconds, and the script will automatically publish English short text to Xiaohongshu periodically.

After the text is posted, you will see the update in [Xiaohongshu](https://creator.xiaohongshu.com/new/note-manager).

### Note
1. When a verification code is required for login, please enter it at the command line and do not perform any operations on the Chrome browser.
2. If you want to use another mobile phone number to login, please delete existing file red_cookies.json firstly.
3. Don't start any other IBM-Granite-v3.1-8B model and Genie API Service when executing this app.
4. The Qwen2.0-7B-SSD model can generate higher-quality content. It is strongly recommended to refer to [大语言模型系列(1): 3分钟上手，在骁龙AI PC上部署DeepSeek!-CSDN博客](https://blog.csdn.net/csdnsqst0050/article/details/149425691) to import the model and run the application with it.