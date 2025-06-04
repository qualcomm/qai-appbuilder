# Langflow package

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
**Langflow** is a low-code development tool designed to simplify the creation and deployment of AI models. The **Langflow package** serves as a reference example, demonstrating how to run Langflow on PCs powered by Snapdragon® X. This package leverages the NPU (Neural Processing Unit) on Snapdragon® X to accelerate AI model inference.

The **Langflow** package includes 3 distinct flows:

1. **YoloV8_det Demo** – An object detection flow based on the YOLOv8 model.
2. **Image Classification Inceptionv3** – An image classification flow using the InceptionV3 model.
3. **Basic Chat Genie** – A conversational flow powered by a locally deployed large language model (LLM).

## ⚠️ Prerequisites


1. **PCs Powered by Snapdragon® X**: Ensure you have a PC powered by Snapdragon® X. For more information on compatible devices, visit [this page](https://www.qualcomm.com/snapdragon/laptops)
2. **PowerShell 7**: Make sure PowerShell 7 is installed on your system.


## 🛠️ Installation Steps 

### 1. Install PowerShell 7  
**Most of the current systems come with PowerShell 5 by default. However, there are numerous download and installation operations in the setup that rely on the unique strategies of PowerShell 7. Therefore, PowerShell 7 must be installed to support it.**

Download and install the appropriate version of PowerShell 7 for your system:  
- [PowerShell 7.5.1 for ARM64](https://github.com/PowerShell/PowerShell/releases/download/v7.5.1/PowerShell-7.5.1-win-arm64.msi) 

For more information, refer to the official Microsoft documentation:  
[Transition from Windows PowerShell 5.1 to PowerShell 7](https://learn.microsoft.com/en-us/powershell/scripting/install/migrate-from-windows-powershell-51-to-powershell-7?view=powershell-7.5)  

### 2. Download the Repository  
#### Option 1: Clone via Git  
1. Install Git: [Download Git for Windows](https://git-scm.com/downloads/win)  
2. Open a Git Bash terminal and run:  
   ```powershell  
   git clone https://github.com/quic/ai-engine-direct-helper.git
   ```  

#### Option 2: Download ZIP  
1. Download the ZIP file: [ai-engine-direct-helper-main.zip](https://github.com/quic/ai-engine-direct-helper/archive/refs/heads/main.zip)  
2. Extract the contents to a local directory.  

### 3. Run Langflow package setup script
**Note that the following operations in the Powershell terminal must be performed as Administrator; otherwise, it will lead to the failure of the process.**

Run PowerShell 7 as **Administrator**. Navigate to the following directory inside the downloaded project:
```powershell
cd <path_to_repo>/tools/langflow/scripts  

.\setup.ps1
```  
- The installation process may take up to 30 minutes.  
- As it will take a relatively long time to download the LLM model during the installation process, please avoid all network fluctuations.
- Avoid using the system during this time. 

**Complete Indicator**: 
If there are no errors during the script operation and the following words are displayed:
```
Install successfully. Press any key to exit...  
```
Then it indicates that the installation is complete.

## Starting the Service

### Option 1: Command execution 

In {your_project_path}\tools\langflow\scripts directory, run the startup script:

```powershell
.\start_langflow.ps1
```
### Option 2: Shortcut
During the above Setup process, a shortcut pointing to the start langflow script will be created on the desktop:
```
Double-click the shortcut named "start_langflow" on the desktop.
```

- Wait a few minutes for all services to start.  
- A browser window should open automatically.  
- If it doesn’t, manually open your browser and go to: [http://127.0.0.1:7860/](http://127.0.0.1:7860/)  

## Using the Flows  

1. On the newly opened LangFlow web page, click the **"New flow"** button to enter the Templates interface.  
2. In the Templates window, click the **"Blank Flow"** button (bottom-right corner) to create a new blank flow.  
3. In the top navigation bar, select **"My Projects"** to access your project directory.  
4. In the left sidebar under **"Folders"**, locate and click the **"Upload a Flow"** icon (usually a folder with an arrow). This will open a file selection dialog.  
5. All flow files are located in the `tools\langflow\flows` directory. Select the desired flow file and click **"Open"** to upload it to your projects.  

### First-Time Setup
1. **Click the “+New Flow” Button**  
   On the main dashboard, click the **`+New Flow`** button to create a new flow.

2. **Select “+Blank Flow”**  
   In the pop-up **Get Started** window, scroll to the bottom-right corner and click **`+Blank Flow`** to start with an empty canvas.

3. **Enter the Flow Design Page**  
   You will be taken to the flow design interface. In the center of the screen, click the **`My Projects`** link to open the project manager.

4. **Upload Flow Templates**  
   In the project manager, click the **upload icon** next to the **Folders** section.

5. **Import Flow Files**  
   Navigate to the directory:
      {your_project_path}/tools/langflow/flows

  Select **all `.json` files** in this folder and click **Open**.

Once uploaded, all reference flows will be imported and available for use in your workspace.

#### 1. Run **YoloV8_det Demo** Flow 
-  From the main dashboard, click **`My Projects`**.

- In the project list, choose **`YoloV8_det Demo`**.

- In the flow editor, locate the **`Qualcomm CV Input`** node. Click on it and upload an image — for example, a photo containing a cat or a dog.

- After uploading the image, click the **arrow button (▶️)** on the **`Qualcomm CV Input`** node to confirm the upload.

- Click the **`Playground`** button in the top-right corner of the interface, then click **`Run Flow`**.

#### 2. Run **Image Classification Inceptionv3** Flow 
- From the main dashboard, click **`My Projects`**.

- In the project list, choose **`Image Classification Inceptionv3`**.

- In the flow editor, locate the **`Qualcomm CV Input`** node. Click on it and upload an image — for example, a photo containing a cat or a dog.

- After uploading the image, click the **arrow button (▶️)** on the **`Qualcomm CV Input`** node to confirm the upload.

- Click the **`Playground`** button in the top-right corner of the interface, then click **`Run Flow`**.

> 🕒 **Note:** On first use, the system will automatically download the Inceptionv3 model in the background. This may take approximately **10 minutes** depending on your network speed. Please be patient.


#### 3. Run **Basic Chat Genie** Flow
-  From the main dashboard, click **`My Projects`**.

-  In the project list, choose **`Basic Chat Genie`**.

-  Click the **`Playground`** button in the top-right corner of the interface.

-  Enter your message in the text input box and click **`Send`**.

The flow will invoke a local large language model to generate a response based on your input, enabling interactive chat functionality.


## Common Issues and Solutions

### Error: "Get-Command : The term 'python' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again."

**Cause**: This error typically occurs due to a conflict between different versions of Python installed on your system.

**Solution**:

1. Uninstall the old version of Python from your system.
2. Re-run the `setup.ps1` script to install the correct version of Python.

### Error: "Failed to spawn: `langflow` Caused by: program not found"

**Cause**: This error indicates that Langflow was not installed successfully.

**Solution**:

1. Run the `setup.ps1` installation script again to ensure Langflow is properly installed.

