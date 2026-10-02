# QAI Runner Skill(internal project name AIPC)

## Overview

This repository includes the **agent skill** for QAIRT model conversion & inference on Qualcomm devices.

After this skill is activated, AI agents can automatically assist you with:
- Model conversion from Pytorch/ONNX to QNN/SNPE formats
- Model deployment on Qualcomm AI PCs and edge devices
- Inference implementation using the qai_appbuilder library

You can also use the [Complete project workflow](#complete-project-workflow) section when you want the agent to execute the project workflow as a whole, rather than assisting step by step.

AIPC (AI Porting Conversion) is the development name. It is released as part of QAI_APPBUILDER, and you may use either term to trigger the skill.

## Publication

**AIPC: Agent-Based Automation for AI Model Deployment with Qualcomm AI Runtime**  
https://arxiv.org/abs/2604.14661

### Disclaimer
> **Disclaimer**  
> This is an experimental feature and still requires further improvement. Code generated with this skill should be treated only as a starting point for development. All generated code must undergo code review, testing, security validation, and any other required software release processes before production use.
---

## Skill Installations

### What is a Skill?

A skill is a reusable package of tools, scripts, and documentation that extends the capabilities of AI agents. Skills are stored in a `skills/` source directory (such as `.cline/skills/`, `.clinerules/skills/`, `.claude/skills/`, `.codex/skills/` or `.qwen/skills/`). They are automatically detected by compatible AI assistants after proper setup and placement in the correct directory location.

---

### How to install skill?

Please install agent skills using your preferred AI models, such as [Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/) or else, you can also install through Visual Studio Code extenstion cline.

You can also use `install skill globally/locally from @skill_source_path`.

---

### Testing the Installations

Test that the skill is working by asking your AI assistant:

**You say:**
> "Do you have the QAI-Runner-Skill available?"

**Alternative Test:**
> "List the tools available in the QAI-Runner-Skill"

---

### Skill Activation

Once installed, the skill is **automatically activated** when:

1. The AI assistant detects the skill, for example `.cline/skills/QAI-Runner-Skill/` in your project or `~/.cline/skills/QAI-Runner-Skill/` globally
2. You mention tasks related to model conversion, ONNX, QNN, or Qualcomm deployment
3. The assistant recognizes keywords like "convert model", "QNN", "Qualcomm AI PC"

---
### Known Behavior Change After Skill Activation

Activating this skill may alter the default behavior of your AI agent. If unintended behavior is observed after activation, disable the skill according to the configuration options of your specific code agent.

### Prerequisites

Ensure the following software is installed before using this skill.


#### 1. [QAIRT SDK](https://quic.github.io/cloud-ai-sdk-pages/latest/qnn-aic/general/QAIRT-SDK-Installation/index.html) (both Target Device, Development Machine)
```bash
# Download from Qualcomm
# Set environment variable
set QNN_SDK_ROOT=/path/to/qnn-sdk
set QAIRT_SDK_ROOT=/path/to/qairt-sdk

# Or Add system variables QNN_SDK_ROOT and QAIRT_SDK_ROOT.
```


#### 2. [qai_appbuilder](https://github.com/qualcomm/qai-appbuilder/releases)  Library (For Inference)
```bash
# Install on target device or development machine
pip install qai_appbuilder
# Or follow Qualcomm's installation instructions
```

---


### Platform-Specific Helper/Prompts (Experimental)

#### Linux
An experimental helper prompt for QAIRT/QAI Appbuilder is available for Linux environments. Please refer to `setup/ubuntu/readme.md` for detailed usage instructions.

#### Windows
For Windows environments, use the following steps:

1. Run `setup/win_installer/Setup_Env.bat` to configure the environment.
2. Run `setup/win_installer/PythonShell.bat` to activate the QAIRT Python virtual environment.
3. In PowerShell, initialize the QAIRT SDK environment by dot-sourcing the setup script:

   ```powershell
   . "$env:QAIRT_SDK_ROOT\bin\envsetup.ps1"
   ```


## Usage

It is recommended to utilize a script that (1) activates the QAIRT Python virtual environment and (2) initializes the QAIRT environment settings. This script serves as the standard procedure for configuring the workspace during validation and testing.

#### Prerequisites
Current tests are performed from a YOLOv8 PyTorch environment. To set up:
```
"Create YOLOv8 PyTorch example and test"
```

### Complete project workflow
This workflow is designed to transform a PyTorch source model and inference flow into QNN or SNPE inference without requiring step-by-step prompts.
Use the following prompts to run a complete AIPC workflow for the current project.

- `"Create an AIPC project in this folder."`
  - Stay in the current source path and create the project in place.
- `"setup project following aipc skill strictly"` for Claude Code
- `"setup project following aipc skill using template strictly"` for Claude Code
  - Claude Code may not create project files from the template automatically. Confirm this manually.

- Adjust the project configuration in `aipc_plan.md`.
  - This is a user action, not a prompt.
  - Regarding "QAIRT_ENV_SETUP": This is a critical configuration item for ensuring agent stability. It is recommended to use a script that automates the activation of a dedicated Python virtual environment containing all necessary packages, followed by the initialization of the QAIRT environment. Automated package installation via the AI agent at runtime is discouraged, as it may result in unintended side effects, such as exceeding context window limits.

- `"Auto-fill any remaining configuration values using derived or default values, then show the project configuration."`
  - Ensure the project configuration is complete before continuing.

- `"update start time"`
  - Use this if you want to track the execution time accurately.

- `"Do all project work."`
  - Execute the full project workflow based on the configured plan.
  - you may use "/goal" to help the work.

Advanced Usage: The AI agent may be instructed to modify the project plan dynamically, such as appending a GUI video inference stage to the conclusion of the workflow.




#### Assistant workflow

Example prompts:

- **ONNX Inference Test**
  - Ensure you have an ONNX model and inference script ready
  - Prompt: `"Use ONNX to inference"`

- **Convert ONNX to SNPE DLC**
  - Prompt: `"I have a real_esrgan_x4plus model(real_esrgan_x4plus.onnx), please convert it to QNN format(.dlc file) for my Qualcomm device"`
  - Converts ONNX model to SNPE DLC format

- **QNN Inference**
  - Prompt: `"I need to use QAI-Runner-Skill to run inference my converted model real_esrgan_x4plus.dlc on current wos device"`
  - Creates Python script using `qai_appbuilder` library
  - Prompt: `"use QAI-Runner-Skill, create qnn inference script from @onnx_inference.py using @yolov8n_a16_w8_qnn_ctx.bin model . follow the guide strictly." `
  - prompt: `"use QAI-Runner-Skill, create qnn inference script from @onnx_inference.py using @yolov8n_a16_w8_qnn_ctx.bin context binary model "`
  - prompt: `"use QAI-Runner-Skill, create snpe inference script from @onnx_inference.py using @yolov8n.dlc model . "`

- **Quantize Model **
  - Prompt: `"Create W8A16 QNN quantization. Use COCO128 for calibration."`

- **Operator Patching**
  - Prompt: `"Follow QAI-Runner-Skill and patch the model."`

  

### Operational Constraints and Reliability

AI agents may exhibit variability in performance. They may occasionally misinterpret instructions, deviate from the intended workflow, or execute unintended actions if not sufficiently constrained.

If the agent deviates from the prescribed instructions, it is recommended to re-issue the request with an explicit constraint prefix. For example:

- `"follow aipc skill" + [your prompt]`

This ensures strict adherence to the AIPC skill workflow and reference protocols.

### Verified Deployment Scenarios

- **WoS (Windows on Snapdragon)**:
  - Full conversion and inference executed on-device (**Snapdragon X Elite Gen 2**).

- **Remote ARM Linux**:
  - **Qualcomm QCS6490**:
    - SNPE model conversion (Floating Point and Quantized) executed on an **x86 host**.
    - Inference executed on the **ARM Linux target (QCS6490)**.
    - *Note: Occasional support issues with FP16 preservation have been observed.*
  - **Qualcomm RB8**:
    - QNN and SNPE model conversion executed on an **x86 host**.
    - Inference executed on the **ARM Linux target (RB8)**.

- **Verified Code Agents**:
  - Codex CLI
  - Cline
  - Qwen Code
  - OpenCode
  - Antigravity CLI/Gemini CLI
  - Claude Code (*Note: Requires additional manual verification as it may default to internal project setup preferences.*)
  - Kilo CLI (*Note: Project setup workflow verification is currently in progress.*)

### Known Issues

- **QCS6490 + QNN Quantization**:
  - Deployment may encounter the following error: `<E> The SocModel doesn't support FP16`.
  - **Root Cause**: The `--preserve_io` flag during conversion may attempt to preserve FP16 precision on SoC configurations where it is not supported.
  - **Workaround**: Utilize the **SNPE DLC** workflow for chipsets lacking FP16 support. Note that similar constraints may apply.
  - **updated solution** : AI will change to  `--preserve_io layout` option.

### Verified Models
- ESRGAN
- LPRNet
- YOLOv8
- YOLOv26
- YOLO-World
- RT-DETRv3 (Requires initial export to ONNX format)
- PaddleOCR v4 (Requires initial export to ONNX format)
- Whisper (Requires structural modifications for NPU optimization prior to initiating the AIPC project workflow)

### Verified LLM

- GPT-5.2-5.5 / Codex
- Claude 4.5/4.6 Sonnet
- Gemini 3.1/3.5 Flash,2.5/3.1 PRO
- DeepSeek-V4 Lite/pro
- Qwen-3.5-Coder-Plus/3.6 plus
- Mimo Pro v2 pro
- Doubao-Seed-2.0-Code
- glm 5.1
- kimi 2.6
- NVIDIA Nemotron-3 pro
- MiniMax 2.5


