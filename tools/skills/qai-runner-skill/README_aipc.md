# AIPC - AI Porting Conversion
![AIPC](../aipc.jpg)


## Overview

AIPC (AI Porting Conversion) is a comprehensive toolkit designed to streamline AI model deployment on Qualcomm platforms. This repository demonstrates how to use the AIPC toolkit as an **agent skill** within AI coding assistant systems, enabling seamless model conversion and deployment across different platforms.

When you activate this skill, AI agents can automatically assist you with:
- Model conversion from ONNX to QNN/SNPE formats
- Model deployment on Qualcomm AI PCs and edge devices
- Inference implementation using the qai_appbuilder library

---
### What is a Skill?

A skill is a reusable package of tools, scripts, and documentation that extends the capabilities of AI agents. Skills are stored in a `skills/` source directory (such as `.cline/skills/`, `.clinerules/skills/`, `.claude/skills/`, or `.codex/skills/`). They are automatically detected by compatible AI assistants after proper setup and placement in the correct directory location.


## Installation

### Install by AI
we suggest you to install using AI prompt. But you must check after install.
- install @skill_source_path  skill globally.
- install @skill_source_path  skill locally.


### Qualcomm Qgenie CLI Skill Installation

The Qualcomm Qgenie CLI provides a convenient command to install skills. When you use the command `install skill from path`, the skill will be installed to the global scope at `~/.config/qgenie-cli/agent/skills/`. For project-specific installations, skills should be placed in the `.codex/skills/` directory within your project folder. Restart CLI after installment is needed.

### Codex CLI Installation Scope: Global vs Project

You can install this skill at two different scopes:


#### Project-Level Installation (Recommended)
- **Location**: `.codex/skills/aipc-toolkit/` in your project directory
- **Scope**: Available only in the specific project
- **Best for**: Project-specific workflows, team collaboration
- **Advantage**: Version control with your project, easy to share with team

#### Global Installation
- **Location**: `~/.codex/skills/aipc-toolkit/` in your home directory
- **Scope**: Available across all your projects
- **Best for**: Personal use across multiple projects
- **Advantage**: Install once, use everywhere


### Qwen code  CLI Skill Installation
https://qwenlm.github.io/qwen-code-docs/zh/users/features/skills/


### Testing the Installation

Test that the skill is working by asking your AI assistant:

**You say:**
> "Do you have the AIPC toolkit skill available?"

**Alternative Test:**
> "List the tools available in the AIPC toolkit skill"

---

### Skill Activation

Once installed, the skill is **automatically activated** when:

1. The AI assistant detects `.codex/skills/aipc-toolkit/` in your project or `~/.codex/skills/aipc-toolkit/` globally
2. You mention tasks related to model conversion, ONNX, QNN, or Qualcomm deployment
3. The assistant recognizes keywords like "convert model", "QNN", "Qualcomm AI PC"

## project workflow 
"" is prompt to fill.

- "create aipc project in this folder" ## you need to stay in source path.
- adjust project config of aipc_plan.md
- "auto fill left config by deriving or default value. show project config."  ## ensure project config.
- "do all work for project"


## Testing

### Automated Testing with AI

The AIPC toolkit can be tested through AI-assisted workflows. The following test scenarios are available:

#### Prerequisites
Current tests are performed from a YOLOv8 PyTorch environment. To set up:
```
"Create YOLOv8 PyTorch example and test"
```

#### Test Scenarios

- **ONNX Inference Test**
  - Ensure you have an ONNX model and inference script ready
  - Prompt: `"Use ONNX to inference"`

- **Convert ONNX to QNN**
  - Prompt: `"Use AIPC, convert ONNX to QNN"`
  - Converts ONNX model to QNN format for Qualcomm hardware

- **Convert ONNX to SNPE DLC**
  - Prompt: `"Use AIPC, convert ONNX to SNPE DLC"`
  - Converts ONNX model to SNPE DLC format

- **Prepare Quantization Input List**
  - Prompt: `"Use AIPC, prepare quantization input list"`
  - Automatically downloads COCO128 dataset
  - Generates raw data and input files

- **Create Quantized QNN Model**
  - Prompt: `"Use AIPC, create quantized QNN model using coco128"`
  - Automatically downloads COCO128 dataset
  - Generates raw data and input files
  - Performs quantization

- **Create Quantized DLC Model**
  - Prompt: `"Use AIPC, create quantized DLC model using coco128"`
  - Automatically downloads COCO128 dataset
  - Generates raw data and input files
  - Performs quantization

- **Create DLC Context Binary**
  - Prompt: `"Use AIPC, create DLC context binary"`
  - Generates context binary file for DLC model deployment

- **QNN Inference**
  the inference code is less stable in qwen code compared to gemini/gpt/claude model.
  - Prompt: `"Use AIPC, use QNN to inference model"`
  - Creates Python script using `qai_appbuilder` library
  - Prompt: `"use AIPC, create qnn inference script from @onnx_inference.py using @yolov8n_a16_w8_qnn_ctx.bin model . follow the guide strictly." `
  - prompt: `"use AIPC, create qnn inference script from @onnx_inference.py using @yolov8n_a16_w8_qnn_ctx.bin context binary model "`
  - prompt: `"use AIPC, create snpe inference script from @onnx_inference.py using @yolov8n.dlc model . "`
  

- **model surgery**
  - very unstable.....using yolo world 
  - to be done.

- **onnx application to snpe application**
create a md, and "read [md] and work".
```
# deploy yolov8 using qualcomm snpe.
you need used aipc toolkit skill.

-  find onnx inference script as [onnx_inference_script]. don't change this file.
- source aienv.ps1 # need setup env.
- test onnx model, save output picture as onnx.jpg
- create snpe model
- create onnx io information.
- run "python aipc [onnx_inference_script]" , save output picture as onnx_aipc.jpg 
```

### Manual Testing and Debugging

For manual testing and debugging:
- Scripts can be called directly from the command line
- For quantization conversion testing, use AI to create input files first
- Review generated files and logs to verify correct operation

---

## Usage Guidelines

### Best Practices

- **Explicit Skill Invocation**
  - Use phrases like `"use AIPC"` or `"use AIPC skill"` to ensure the AI assistant activates the skill
  - This enhances attention and ensures proper tool selection

- **Project Documentation**
  - Create a project markdown file documenting your workflow steps
  - Don't rely solely on AIPC to handle the entire flow
  - Break down complex tasks into manageable steps

- **Workflow Planning**
  - Plan your conversion pipeline before execution
  - Document expected inputs and outputs
  - Verify each step before proceeding to the next

- **Start with Float Models**
  - Always test with floating-point (FP32/FP16) models first before quantization
  - Quantized models require more time and effort to test and debug
  - AI-assisted quantization workflows can be less stable and may need manual intervention
  - Validate your model works correctly in float precision before attempting quantization

### Example Workflow

```markdown
# My Model Conversion Project

## Steps
1. Export PyTorch model to ONNX
2. Validate ONNX model with test inference
3. Convert ONNX to QNN format
4. Prepare calibration data (if quantization needed)
5. Quantize model
6. Test inference with QNN model
```

---

## Important Notices

### Monitoring and Verification

⚠️ **Always verify that AIPC toolkit is being used correctly:**
- Check that expected operations are performed using AIPC toolkit
- Monitor AI assistant actions to ensure it's calling the skill correctly
- The AI may not always use the AIPC skill automatically - manual verification is required

### Data Management

⚠️ **Keep your workspace clean:**
- Don't place unrelated data in the working folder
- The AI assistant may detect and attempt to use existing data files
- Example: It might use previously generated results instead of creating new ones
- Remove old conversion artifacts before starting new conversions

### Script and Documentation Integrity

⚠️ **Protect skill files:**
- The AI assistant may modify skill documentation and scripts
- The AI may perform actions beyond your explicit requests
- Monitor changes to skill files carefully
- Consider version control for skill files

### Installation Behavior

⚠️ **Installation scripts:**
- The AI may run installation scripts even if dependencies are already installed
- Review installation commands before execution
- Use manual installation when appropriate

---

## Troubleshooting

### Common Issues

1. **Skill Not Activated**
   - Verify skill is installed in correct directory
   - Use explicit phrases: "use AIPC skill"
   - Restart your AI assistant/CLI

2. **Wrong Data Being Used**
   - Clean working directory of old artifacts
   - Specify exact file paths in commands
   - Remove previous conversion results

3. **Unexpected Modifications**
   - Review AI actions before approval
   - Use version control to track changes
   - Keep backups of important files

---

## Additional Resources

- **SKILL.md**: Detailed skill configuration and capabilities
- **scripts/**: Conversion scripts and utilities
- **references/**: Technical documentation and guides
- **Quantization Guide**: See `docs/QUANTIZATION_GUIDE.md`
- **Calibration Data**: See `docs/CALIBRATION_DATA_PREPARATION.md`

---

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review skill documentation in `SKILL.md`
3. Examine reference documentation in `references/`
4. Test with manual script execution for debugging
