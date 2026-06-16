# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import onnxruntime as ort
import onnx
import os
import sys
import yaml
import argparse


def _normalize_shape_for_yaml(shape):
    """Convert ORT shape to YAML-friendly list.

    - int-like dims -> int
    - dynamic/symbolic dims -> -1
    """
    out = []
    for dim in shape:
        try:
            out.append(int(dim))
        except Exception:
            out.append(-1)
    return out

def inspect_onnx(model_path, include_onnx_dims=False):
    print(f"\n{'='*60}")
    print(f"Inspecting: {model_path}")
    print(f"{'='*60}")
    
    try:
        # Load with ONNX Runtime for reliable shape inference
        session = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    # Keep legacy keys (`input`/`output`) for backward compatibility,
    # and add rich keys for wrapper/layout-aware usage.
    yaml_data = {
        'input': [],
        'output': [],
        'inputs': [],
        'outputs': [],
    }
    if include_onnx_dims:
        yaml_data['onnx_dims'] = {
            'inputs': {},
            'outputs': {},
        }

    print("\n[INPUTS]")
    for input_meta in session.get_inputs():
        name = input_meta.name
        shape = input_meta.shape
        type_name = input_meta.type
        
        print(f"  Name: {name}")
        print(f"  Shape: {shape}")
        print(f"  Type: {type_name}")
        print("-" * 30)
        
        norm_shape = _normalize_shape_for_yaml(shape)
        yaml_data['input'].append(name)
        yaml_data['inputs'].append({
            'name': name,
            'onnx_shape': list(norm_shape),
            'dtype': type_name,
        })
        if include_onnx_dims:
            yaml_data['onnx_dims']['inputs'][name] = list(norm_shape)

    print("\n[OUTPUTS]")
    for output_meta in session.get_outputs():
        name = output_meta.name
        shape = output_meta.shape
        type_name = output_meta.type
        
        print(f"  Name: {name}")
        print(f"  Shape: {shape}")
        print(f"  Type: {type_name}")
        print("-" * 30)

        norm_shape = _normalize_shape_for_yaml(shape)
        yaml_data['output'].append(name)
        yaml_data['outputs'].append({
            'name': name,
            'onnx_shape': list(norm_shape),
            'dtype': type_name,
        })
        if include_onnx_dims:
            yaml_data['onnx_dims']['outputs'][name] = list(norm_shape)
        
    # Generate YAML file
    base_name = os.path.splitext(model_path)[0]
    yaml_path = f"{base_name}.yaml"
    
    try:
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(yaml_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        print(f"\nGenerated YAML file: {yaml_path}")
    except Exception as e:
        print(f"\nFailed to write YAML file: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="Inspect ONNX I/O and generate YAML config."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="ONNX files to inspect. If omitted, inspect all .onnx files in current directory."
    )
    parser.add_argument(
        "--include-onnx-dims",
        action="store_true",
        help="Include duplicated onnx_dims mapping block in YAML output (default: off)."
    )
    args = parser.parse_args()

    # If arguments are provided, use those. Otherwise look for .onnx files in current dir.
    if args.files:
        files = args.files
    else:
        files = [f for f in os.listdir('.') if f.endswith('.onnx')]
        files.sort()
        
    if not files:
        print("No ONNX files found in the current directory.")
        return

    print(f"Found {len(files)} ONNX file(s) to process.")
    
    for f in files:
        if os.path.exists(f):
            inspect_onnx(f, include_onnx_dims=args.include_onnx_dims)
        else:
            print(f"File not found: {f}")

if __name__ == "__main__":
    main()
