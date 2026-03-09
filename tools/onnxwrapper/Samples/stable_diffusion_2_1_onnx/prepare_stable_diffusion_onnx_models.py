#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# ---------------------------------------------------------------------
# Download Stable Diffusion 2.1 ONNX models (with optional external data)
#
#
# Requirements:
#   pip install -U huggingface_hub onnx
# ---------------------------------------------------------------------

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


REPO_ID = "aislamov/stable-diffusion-2-1-base-onnx"


def _model_requires_external_data(onnx_path: Path) -> tuple[bool, set[str]]:
    """
    Return (requires_external_data, locations)
    locations: set of external data filenames referenced by the model.
    """
    import onnx
    from onnx import TensorProto

    # Load without external data (we only need the metadata that says whether it uses external data)
    m = onnx.load(str(onnx_path), load_external_data=False)

    referenced = set()

    def collect_from_tensor(t):
        # TensorProto has data_location + external_data entries when external
        if isinstance(t, TensorProto) and t.data_location == TensorProto.EXTERNAL:
            # external_data is a repeated key/value pair list
            loc = None
            for kv in t.external_data:
                if kv.key == "location":
                    loc = kv.value
                    break
            if loc:
                referenced.add(loc)

    # Check initializers
    for init in m.graph.initializer:
        collect_from_tensor(init)

    # Check constant nodes (rare but possible)
    for node in m.graph.node:
        if node.op_type == "Constant":
            for attr in node.attribute:
                if attr.type == onnx.AttributeProto.TENSOR:
                    collect_from_tensor(attr.t)

    return (len(referenced) > 0), referenced


def download_onnx_models(out_root: Path):
    print(f"[HF] Downloading ONNX models from: {REPO_ID}")
    print(f"[HF] Output directory: {out_root}")

    # Download ONNX + external data files (if they exist)
    allow_patterns = [
        "**/*.onnx",
        "**/*.onnx_data",
        "**/*.json",
        "**/*.txt",
        "**/*.md",
    ]

    snapshot_download(
        repo_id=REPO_ID,
        local_dir=str(out_root),
        local_dir_use_symlinks=False,
        allow_patterns=allow_patterns,
    )
    print("[HF] Download completed")

    # ------------------------------------------------------------
    # Post-check: only require external data when ONNX references it
    # ------------------------------------------------------------
    print("[CHECK] Verifying ONNX external data references...")

    missing = []
    checked = 0

    for onnx_path in sorted(out_root.rglob("*.onnx")):
        checked += 1
        requires_ext, locations = _model_requires_external_data(onnx_path)

        if not requires_ext:
            print(f"[OK] {onnx_path.relative_to(out_root)} (no external data)")
            continue

        # If the model references external data, ensure all referenced files exist
        all_ok = True
        for loc in locations:
            data_path = (onnx_path.parent / loc).resolve()
            if not data_path.exists():
                all_ok = False
                missing.append(str(data_path))
        if all_ok:
            locs = ", ".join(sorted(locations))
            print(f"[OK] {onnx_path.relative_to(out_root)} (external data: {locs})")
        else:
            print(f"[ERROR] {onnx_path.relative_to(out_root)} references missing external data: {locations}")

    print(f"[CHECK] Scanned {checked} ONNX files.")
    if missing:
        raise RuntimeError(
            "Some ONNX models reference external data files that are missing:\n"
            + "\n".join(missing)
            + "\n\nThis is required by ONNX external data format. "
              "Ensure the referenced files exist in the same folder as model.onnx. "
              "See ONNX external data spec for details."
        )

    print("[CHECK] All required external data files are present ✅")


def main():
    parser = argparse.ArgumentParser(
        description="Download Stable Diffusion 2.1 ONNX models (with optional external data)"
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        default="models-onnx/aislamov_stable-diffusion-2-1-base-onnx",
        help="Output directory",
    )
    args = parser.parse_args()

    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    download_onnx_models(out_root)

    print("\n[OK] ONNX model preparation finished.")
    print(f"Model root: {out_root}")


if __name__ == "__main__":
    main()