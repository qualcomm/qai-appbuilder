# QAI Runner Skill

## Overview

This repository includes the **agent skill** for QAIRT model conversion & inference on Qualcomm devices.

After you activated this skill, AI agents can automatically assist you with:
- Model conversion from ONNX to QNN/SNPE formats
- Model deployment on Qualcomm AI PCs and edge devices
- Inference implementation using the qai_appbuilder library

---

## Skill Installations

### What is a Skill?

A skill is a reusable package of tools, scripts, and documentation that extends the capabilities of AI agents. Skills are stored in a `skills/` source directory (such as `.cline/skills/`, `.clinerules/skills/`, `.claude/skills/`, `.codex/skills/` or `.qwen/skills/`). They are automatically detected by compatible AI assistants after proper setup and placement in the correct directory location.

---

### How to install skill?

Please install agent skills using your preferred AI models, such as [Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/) or else, you can also install through Visual Studio Code extenstion cline.

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

### Prerequisites After Installation

After installing the skill, ensure you have the required software:


#### 1. [QAIRT SDK](https://quic.github.io/cloud-ai-sdk-pages/latest/qnn-aic/general/QAIRT-SDK-Installation/index.html) (both Target Device, Development Machine)
```bash
# Download from Qualcomm
# Set environment variable
set QNN_SDK_ROOT=/path/to/qnn-sdk
set QAIRT_SDK_ROOT=/path/to/qairt-sdk

# Or Add system variables QNN_SDK_ROOT and QAIRT_SDK_ROOT.
```


#### 2. [qai_appbuilder](https://github.com/quic/ai-engine-direct-helper/releases)  Library (For Inference)
```bash
# Install on target device or development machine
pip install qai_appbuilder
# Or follow Qualcomm's installation instructions
```

---


## Testing



### Automated Testing with AI

The QAI-Runner-Skill can be tested through AI-assisted workflows. The following test scenarios are available:

#### Prerequisites
Current tests are performed from a YOLOv8 PyTorch environment. To set up:
```
"Create YOLOv8 PyTorch example and test"
```

#### Test Scenarios

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
  
