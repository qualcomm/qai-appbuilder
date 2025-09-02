# Langflow package

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
**Langflow** is a low-code development tool designed to simplify the creation and deployment of AI models. The **Langflow package** serves as a reference example, demonstrating how to run Langflow on PCs powered by Snapdragon® X. This package leverages the NPU (Neural Processing Unit) on Snapdragon® X to accelerate AI model inference.

The **Langflow** package includes 3 distinct flows:

1. **YoloV8_det Demo** – An object detection flow based on the YOLOv8 model.
2. **Image Classification Inceptionv3** – An image classification flow using the InceptionV3 model.
3. **Basic Chat Genie** – A conversational flow powered by a locally deployed large language model (LLM).
4. **LLM CV Chat Genie** - The CV models interactive conversational flow supported by a large language model (LLM).

## ⚠️ Prerequisites


1. **PCs Powered by Snapdragon® X**: Ensure you have a PC powered by Snapdragon® X. For more information on compatible devices, visit [this page](https://www.qualcomm.com/snapdragon/laptops)
2. **PowerShell 7**: Make sure PowerShell 7 is installed on your system.


## 🛠️ Installation Steps 

### 1. Install PowerShell 7  
**Most of the current systems come with PowerShell 5 by default. However, there are numerous download and installation operations in the setup that rely on the unique strategies of PowerShell 7. Therefore, PowerShell 7 must be installed to support it.**

Download and install the appropriate version of PowerShell 7 for your system:  
- [PowerShell 7.5.1 for ARM64](https://github.com/PowerShell/PowerShell/releases/download/v7.5.1/PowerShell-7.5.1-win-arm64.msi) 


### 2.  Quick Installation
For a quick installation of the langflow environment, please refer to: [launcher-README.md](https://github.com/quic/ai-engine-direct-helper/blob/main/tools/launcher/README.md)

## Using the Flows  

1. On the newly opened LangFlow web page, click the **"New flow"** button to enter the Templates interface.  
2. In the Templates window, click the **"Blank Flow"** button (bottom-right corner) to create a new blank flow.  
3. In the top navigation bar, select **"My Projects"** to access your project directory.  
4. In the left sidebar under **"Folders"**, locate and click the **"Upload a Flow"** icon (usually a folder with an arrow). This will open a file selection dialog.  
5. All flow files are located in the `tools\langflow\flows` directory. Select the desired flow file and click **"Open"** to upload it to your projects.  

### First-Time Setup
1. **Click the “+New Flow” Button**  
   On the main dashboard, click the **`+New Flow`** button to create a new flow.

2. **Select “+Blank Flow”**  
   In the pop-up **Get Started** window, scroll to the bottom-right corner and click **`+Blank Flow`** to start with an empty canvas.

3. **Enter the Flow Design Page**  
   You will be taken to the flow design interface. In the center of the screen, click the **`My Projects`** link to open the project manager.

4. **Upload Flow Templates**  
   In the project manager, click the **upload icon** next to the **Folders** section.

5. **Import Flow Files**  
   Navigate to the directory:
      {your_project_path}/tools/langflow/flows

  Select **all `.json` files** in this folder and click **Open**.

Once uploaded, all reference flows will be imported and available for use in your workspace.

#### 1. Run **YoloV8_det Demo** Flow 
-  From the main dashboard, click **`My Projects`**.

- In the project list, choose **`YoloV8_det Demo`**.

- In the flow editor, locate the **`Qualcomm CV Input`** node. Click on it and upload an image — for example, a photo containing a cat or a dog.

- After uploading the image, click the **arrow button (▶️)** on the **`Qualcomm CV Input`** node to confirm the upload.

- Click the **`Playground`** button in the top-right corner of the interface, then click **`Run Flow`**.

#### 2. Run **Image Classification Inceptionv3** Flow 
- From the main dashboard, click **`My Projects`**.

- In the project list, choose **`Image Classification Inceptionv3`**.

- In the flow editor, locate the **`Qualcomm CV Input`** node. Click on it and upload an image — for example, a photo containing a cat or a dog.

- After uploading the image, click the **arrow button (▶️)** on the **`Qualcomm CV Input`** node to confirm the upload.

- Click the **`Playground`** button in the top-right corner of the interface, then click **`Run Flow`**.

> 🕒 **Note:** On first use, the system will automatically download the Inceptionv3 model in the background. This may take approximately **10 minutes** depending on your network speed. Please be patient.


#### 3. Run **Basic Chat Genie** Flow
-  From the main dashboard, click **`My Projects`**.

-  In the project list, choose **`Basic Chat Genie`**.

-  Click the **`Playground`** button in the top-right corner of the interface.

-  Enter your message in the text input box and click **`Send`**.

The flow will invoke a local large language model to generate a response based on your input, enabling interactive chat functionality.

#### 4. Run **LLM CV Chat Genie** Flow
-  From the main dashboard, click **`My Projects`**.

-  In the project list, choose **`LLM CV Demo`**.

-  Select the corresponding image upload in the **`Qualcomm CV Input`** module of the conversational flow.

-  Click the **`Playground`** button in the top-right corner of the interface.

-  Enter your message in the text input box and click **`Send`**.

> 🕒 **Note1:** For the first use, please note: You need to download and extract the [Qwen2.0-7B-SSD](https://www.aidevhome.com/data/adh2/models/8380/qwen2_7b_ssd.zip) model in advance and store all files inside in the `ai-engine-direct-helper\samples\genie\python\models\Qwen2.0-7B-SSD` directory.

> 🕒 **Note2:** If you want flow to handle images correctly as required, it is necessary to note that the message should include the corresponding `prompt information`. For specific prompt information, please refer to the **`Prompt`** module of the conversational flow.
