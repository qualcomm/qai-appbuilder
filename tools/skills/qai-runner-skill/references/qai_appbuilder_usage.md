# Guide: Generic Usage of qai_appbuilder

This document provides a general guide on how to use the `qai_appbuilder` library for running inference on a QNN model.

## 1. Core Concepts

The `qai_appbuilder` library provides a high-level API to interact with the Qualcomm AI Engine. The key components are:

-   `QNNConfig`: For setting up the global execution environment.
-   `QNNContext`: A class that you inherit from to create a wrapper around your model.
-   `PerfProfile`: For controlling device performance modes during execution.

## 2. Step-by-Step Guide

### Step 1: Configure the Environment

Before performing any inference, the QNN environment must be initialized. This is done once per application session.

```python
from qai_appbuilder import QNNConfig, Runtime, LogLevel, ProfilingLevel

QNNConfig.Config(
    runtime=Runtime.HTP,
    log_level=LogLevel.WARN,
    profiling_level=ProfilingLevel.BASIC
)
```

You can specify an absolute path to the QNN libraries. Set to empty string to use default setting.

### Step 2: Create a Model Context Class

Create a custom class that inherits from `QNNContext`. This class will represent your model. You can override the `Inference` method to handle model-specific input and output processing if needed.

```python
from qai_appbuilder import QNNContext

class MyModel(QNNContext):
    def __init__(self, model_name: str, model_path: str):
        # Call the parent constructor
        super().__init__(model_name, model_path)

    def Inference(self, input_tensors: list):
        # The base Inference method expects a list of input tensors
        output_tensors = super().Inference(input_tensors)
        return output_tensors
```

### Step 3: Instantiate and Use the Model

Instantiate your custom model class with the model's name and the path to its `.bin` or `.so` or `.dll` file.
The `.so`,`.dll` model's basename is derived from the original ONNX model name, prefixed with `lib`.
If a QNN model is provided as a `.so`,`.dll` file, use the corresponding content binary with a `.bin` postfix.

the SNPE dlc model is supported with file name `.dlc` or `.dlc.bin` 

```python
# Path to the compiled QNN model
model_path = "path/to/your/model.bin"

# Create an instance of your model
my_model = MyModel("my_model_instance", model_path)
```

### Step 4: Preprocess Data and Run Inference

Prepare your input data as a list of NumPy arrays. Then, call the inference method. For optimal performance, you can set the performance profile to `BURST` before inference.

```python
import numpy as np
from qai_appbuilder import PerfProfile

# Assume input_data is a NumPy array matching the model's input shape
input_data = np.random.rand(1, 224, 224, 3).astype(np.float32)

# Set performance to BURST mode
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Run inference
# Note: The input must be a list of numpy arrays
raw_outputs = my_model.Inference([input_data])

# Revert performance profile after inference
PerfProfile.RelPerfProfileGlobal()
```

The `raw_outputs` variable will be a list of NumPy arrays, with each array corresponding to an output tensor of the model.

### Step 5: Post-process Results

Process the raw output tensors according to your model's logic.

```python
# Example: Process the first output tensor
results = raw_outputs[0]
# ... your post-processing logic here ...
```

### Step 6: Release Resources

When you are finished with the model, ensure you release the associated resources by deleting the model object. This is crucial for freeing up hardware resources.

```python
# Release the model and its resources
del my_model
```

This completes the basic workflow for using the `qai_appbuilder` library to run inference.

### Important: Data Layout and Model Conversion

When converting models from other frameworks to QNN, data layout discrepancies may arise. We strongly recommend using the `--preserve_io` option during model conversion. This flag ensures that the input and output layouts of the converted model match those of the original model. As a result, you can reuse your existing preprocessing and postprocessing code without needing to adjust for QNN-specific layout changes.

Sometimes, QNN conversion changes the order of inputs and outputs. In such cases, you can use a configuration file (e.g., YAML) to correct the output order after inference.

The following example demonstrates how to implement a custom model class that loads an I/O configuration and reorders the output tensors accordingly.

```python
import os
import yaml
import logging
from qai_appbuilder import QNNContext

logger = logging.getLogger(__name__)

class MyModel(QNNContext):
    def __init__(self, model_name: str, model_path: str):
        # Call the parent constructor
        model_path = os.path.abspath(model_path)  # prev local file issue
        super().__init__(model_name, model_path)
        self.io_config = None
        
        # Check for YAML config file
        # Priority 1: same base name as model_path (e.g. path/to/model.so -> path/to/model.yaml)
        # Priority 2: model_name.yaml (e.g. language_encoder.yaml)
        yaml_paths = [os.path.splitext(model_path)[0] + ".yaml", model_name + ".yaml"]
        
        for yaml_path in yaml_paths:
            if os.path.exists(yaml_path):
                try:
                    with open(yaml_path, 'r') as f:
                        self.io_config = yaml.safe_load(f)
                    logger.info(f"Loaded IO config from {yaml_path}")
                    break
                except Exception as e:
                    logger.warning(f"Failed to load IO config from {yaml_path}: {e}")
            else:
                logger.debug(f"No IO config found at {yaml_path}")
        else:
            logger.info(f"No IO config found. Checked: {yaml_paths}")

    def _reorder_tensors(self, tensors, current_names, desired_names, tag=""):
        if len(current_names) == len(tensors):
            tensor_map = {name: tensor for name, tensor in zip(current_names, tensors)}
            try:
                return [tensor_map[name] for name in desired_names]
            except KeyError as e:
                logger.warning(f"{tag} name {e} not found in available names: {current_names}. Returning original order.")
        else:
            logger.warning(f"Number of {tag} names ({len(current_names)}) does not match tensors ({len(tensors)})")
        return tensors

    def Inference(self, input_tensors: list):
        # input_tensors is a list of numpy arrays
        if self.io_config is not None and "input" in self.io_config:
            if isinstance(self.io_config["input"], list):
                input_tensors = self._reorder_tensors(input_tensors, self.io_config["input"], self.getInputName(), "Input")

        output_tensors = super().Inference(input_tensors)
        
        if self.io_config is not None and "output" in self.io_config:
            if isinstance(self.io_config["output"], list):
                output_tensors = self._reorder_tensors(output_tensors, self.getOutputName(), self.io_config["output"], "Output")
        
        return output_tensors
```
