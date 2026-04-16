
import argparse
import os
import numpy as np

from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
    DataType,
)


class QNNModel(QNNContext):
    def Inference(self, input_data):
        output_data = super().Inference(input_data)
        return output_data


qnn_dir = ""
QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)


def _numpy_dtype_from_str(s: str):
    s = (s or "").strip().lower()
    mapping = {
        "float32": np.float32,
        "float": np.float32,
        "fp32": np.float32,
        "float16": np.float16,
        "half": np.float16,
        "fp16": np.float16,
        "int8": np.int8,
        "uint8": np.uint8,
        "int16": np.int16,
        "uint16": np.uint16,
        "int32": np.int32,
        "uint32": np.uint32,
        "int64": np.int64,
        "uint64": np.uint64,
    }
    return mapping.get(s, None)


def _numpy_dtype_from_qnn_dtype(qnn_dt):
    """
    Best-effort mapping from QAI AppBuilder DataType (or its string form) to numpy dtype.
    If qnn_dt is a list/tuple, use the first element.
    """
    if isinstance(qnn_dt, (list, tuple)) and len(qnn_dt) > 0:
        qnn_dt = qnn_dt[0]

    try:
        if qnn_dt == DataType.FLOAT:
            return np.float32

        if hasattr(DataType, "HALF") and qnn_dt == DataType.HALF:
            return np.float16
        if hasattr(DataType, "FLOAT16") and qnn_dt == DataType.FLOAT16:
            return np.float16

        if hasattr(DataType, "INT32") and qnn_dt == DataType.INT32:
            return np.int32
        if hasattr(DataType, "INT64") and qnn_dt == DataType.INT64:
            return np.int64
        if hasattr(DataType, "INT16") and qnn_dt == DataType.INT16:
            return np.int16
        if hasattr(DataType, "INT8") and qnn_dt == DataType.INT8:
            return np.int8
        if hasattr(DataType, "UINT8") and qnn_dt == DataType.UINT8:
            return np.uint8
    except Exception:
        pass

    # Fallback: string-based guess
    s = str(qnn_dt).lower()
    if "float16" in s or "fp16" in s or "half" in s:
        return np.float16
    if "float32" in s or "fp32" in s or "float" in s:
        return np.float32
    if "int32" in s:
        return np.int32
    if "int64" in s:
        return np.int64
    if "int16" in s:
        return np.int16
    if "int8" in s:
        return np.int8
    if "uint8" in s:
        return np.uint8
    return None


def main(model_path, raw_paths=None, io_data_type="float", raw_dtype=None, keep_shape=False):
    print()
    print("model path:", model_path)
    print()

    # When io_data_type is 'native', pass DataType.NATIVE to AppBuilder.
    if io_data_type == "native":
        model = QNNModel(
            "model",
            model_path,
            input_data_type=DataType.NATIVE,
            output_data_type=DataType.NATIVE,
        )
    else:
        model = QNNModel("model", model_path)

    print()
    print("input:")
    input_shapes = model.getInputShapes()
    print("shape", input_shapes)
    input_dataType = model.getInputDataType()
    print("datatype", input_dataType)
    print()

    print("outputs:")
    output_shapes = model.getOutputShapes()
    print("shape", output_shapes)
    output_dataType = model.getOutputDataType()
    print("datatype", output_dataType)

    input_data = []

    # Decide numpy dtype for raw/random input
    np_dtype = _numpy_dtype_from_str(raw_dtype) if raw_dtype else _numpy_dtype_from_qnn_dtype(input_dataType)
    if np_dtype is None:
        np_dtype = np.float32
        print("[WARN] Cannot infer input numpy dtype from model datatype; fallback to float32.")

    if raw_paths:
        paths = [p.strip() for p in raw_paths.split(";") if p.strip()]
        print("raw:")
        for i, path in enumerate(paths):
            data = np.fromfile(path, dtype=np_dtype)

            if keep_shape:
                # Best-effort reshape to the model's expected input shape
                try:
                    exp_shape = input_shapes[i]
                    exp_size = int(np.prod(exp_shape))
                    if data.size == exp_size:
                        data = data.reshape(exp_shape)
                    else:
                        print(
                            f"[WARN] keep_shape enabled but raw size mismatch for input[{i}]: "
                            f"raw_elems={data.size}, expected={exp_size}. Keep as 1D."
                        )
                except Exception as e:
                    print(f"[WARN] keep_shape reshape failed for input[{i}]: {e}. Keep as 1D.")
            else:
                data = data.reshape(data.size)

            print(data.shape)
            input_data.append(data)
    else:
        print("random:")
        for shape in input_shapes:
            if np.issubdtype(np_dtype, np.floating):
                data = np.random.rand(*shape).astype(np_dtype)
            else:
                # For integer types, generate a small-range random integer tensor
                data = np.random.randint(low=0, high=4, size=shape, dtype=np_dtype)

            if not keep_shape:
                data = data.reshape(data.size)

            print(data.shape)
            input_data.append(data)

    output = model.Inference(input_data)

    print()
    print("output shape:")
    index = 0
    for out in output:
        out = out.reshape(output_shapes[index])
        output_raw_path = "image_embeds_" + str(index) + ".bin"
        index += 1
        print(out.shape)
        out.tofile(output_raw_path)
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single model path.")
    parser.add_argument("--model_path", help="Path to the model", default=None)
    parser.add_argument("--raw_paths", help="Path to raw", default=None)

    parser.add_argument(
        "--io_data_type",
        choices=["float", "native"],
        default="float",
        help="I/O data type policy: 'float' uses default (typically DataType.FLOAT); 'native' uses DataType.NATIVE for both input/output.",
    )
    parser.add_argument(
        "--raw_dtype",
        default=None,
        help="Numpy dtype for raw input files (e.g., float32/float16/int32/int16/int8/uint8). If omitted, will try to infer from model input datatype.",
    )
    parser.add_argument(
        "--keep_shape",
        action="store_true",
        help="Keep input tensors in their original shapes. By default inputs are flattened to 1D buffers.",
    )

    args = parser.parse_args()
    main(
        args.model_path,
        args.raw_paths,
        io_data_type=args.io_data_type,
        raw_dtype=args.raw_dtype,
        keep_shape=args.keep_shape,
    )
