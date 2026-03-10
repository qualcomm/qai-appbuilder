# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import onnxruntime as ort
import onnx
import os
import sys
import yaml

def inspect_onnx(model_path):
    print(f"\n{'='*60}")
    print(f"Inspecting: {model_path}")
    print(f"{'='*60}")
    
    try:
        # Load with ONNX Runtime for reliable shape inference
        session = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    yaml_data = {'input': [], 'output': []}

    print("\n[INPUTS]")
    for input_meta in session.get_inputs():
        name = input_meta.name
        shape = input_meta.shape
        type_name = input_meta.type
        
        print(f"  Name: {name}")
        print(f"  Shape: {shape}")
        print(f"  Type: {type_name}")
        print("-" * 30)
        
        yaml_data['input'].append(name)

    print("\n[OUTPUTS]")
    for output_meta in session.get_outputs():
        name = output_meta.name
        shape = output_meta.shape
        type_name = output_meta.type
        
        print(f"  Name: {name}")
        print(f"  Shape: {shape}")
        print(f"  Type: {type_name}")
        print("-" * 30)

        yaml_data['output'].append(name)
        
    # Generate YAML file
    base_name = os.path.splitext(model_path)[0]
    yaml_path = f"{base_name}.yaml"
    
    try:
        with open(yaml_path, 'w') as f:
            yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False)
        print(f"\nGenerated YAML file: {yaml_path}")
    except Exception as e:
        print(f"\nFailed to write YAML file: {e}")

def main():
    # If arguments are provided, use those. Otherwise look for .onnx files in current dir.
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        files = [f for f in os.listdir('.') if f.endswith('.onnx')]
        files.sort()
        
    if not files:
        print("No ONNX files found in the current directory.")
        return

    print(f"Found {len(files)} ONNX file(s) to process.")
    
    for f in files:
        if os.path.exists(f):
            inspect_onnx(f)
        else:
            print(f"File not found: {f}")

if __name__ == "__main__":
    main()
