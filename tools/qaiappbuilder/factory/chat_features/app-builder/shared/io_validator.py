# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
io_validator
============
Single source of truth for tensor I/O contracts that bridges ModelBuilder
(qai_pack_export.py) and AppBuilder (runner.py) so that any model converted
+ validated in ModelBuilder is **guaranteed** to run in AppBuilder.

Why this module exists
----------------------
A native ``ctx.run([tensor])`` call in ``qai_appbuilder`` performs an
unchecked memcpy on the underlying buffer assuming the caller passed a
contiguous tensor of the exact shape/dtype the model graph expects. If
the caller is off in **any** of these dimensions:

    shape  (e.g. 224×224 vs 299×299)
    dtype  (e.g. float64 vs float32)
    layout (e.g. NHWC vs NCHW)
    contiguity (e.g. a transposed view)
    byte size (== prod(shape) × dtype.itemsize)

…the result is an undebuggable VCRUNTIME140 access violation
(``0xC0000005``), no Python traceback, empty stderr.

The contract & validator below makes every one of those a typed
``IOContractError`` that the orchestrator can render to the user as a
concrete actionable message. The single chokepoint is
:func:`validate_inputs` — every runner MUST call it immediately before
``ctx.run``.

The contract format
-------------------
``io_contract`` is a dict that lives in two places:

* ``manifest.json["io_contract"]`` — written by ``qai_pack_export.py`` after
  it actually loaded the .bin file and queried the qai_appbuilder native
  API. This is the *static* contract.

* The runner extracts a *live* contract by calling
  :func:`extract_io_contract` after it loads the .bin. This is the
  *runtime* contract.

The two MUST agree (see :func:`assert_contracts_compatible`). If they
diverge, the .bin has been swapped or the manifest is stale.

Schema (minimal but complete)::

    {
      "schema_version": 1,
      "graph_name": "inception_v3",
      "inputs":  [ <tensor_spec>, ... ],   # ordered as ctx.run expects
      "outputs": [ <tensor_spec>, ... ],
      "validated_at_export": true,
    }

    <tensor_spec> = {
      "name":    "input",
      "dtype":   "float32",                # see _DTYPE_MAP
      "shape":   [1, 3, 299, 299],         # None on a dimension means dynamic
      "byte_size": 1072812,                # prod(shape) * itemsize, only when fully static
      "layout":  "NCHW",                   # heuristic, optional, advisory
    }

Public API
----------
* :class:`IOContractError`           — raised by validator; carries .code / .detail
* :func:`extract_io_contract`        — query a live qai_appbuilder.QNNContext for its contract
* :func:`assert_contracts_compatible`— cross-check static vs live contracts
* :func:`validate_inputs`            — final chokepoint before ctx.run
* :func:`prepare_input_for_contract` — re-cast / re-layout / contig'fy a single tensor
"""
from __future__ import annotations

import json
from typing import Any, Iterable

import numpy as np


# ──────────────────────────────────────────────────────────────────────────
# Public exception
# ──────────────────────────────────────────────────────────────────────────
class IOContractError(Exception):
    """Raised by the validator when an array fails contract checks.

    Attributes
    ----------
    code : str
        Stable, machine-readable error code. Renderable in UI and i18n-able.
        Members of the closed set:

            INVALID_INPUT_DTYPE      element dtype incompatible
            INVALID_INPUT_RANK       wrong number of dimensions
            INVALID_INPUT_SHAPE      static-axis size mismatch
            INVALID_INPUT_BYTES      total byte count mismatch
            INVALID_INPUT_COUNT      wrong number of input tensors
            CONTRACT_MISMATCH        static contract != live contract
            CONTRACT_MISSING         neither static nor live contract available
    message : str
        Human-readable summary.
    detail : dict
        Machine-readable diff: usually ``{"got_shape": ..., "expected_shape": ...}``
        or similar. Always JSON-serializable.
    """

    __slots__ = ("code", "message", "detail")

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"IOContractError(code={self.code!r}, message={self.message!r})"


# ──────────────────────────────────────────────────────────────────────────
# dtype <-> numpy translation
# ──────────────────────────────────────────────────────────────────────────
_DTYPE_MAP: dict[str, np.dtype] = {
    "float32": np.dtype(np.float32),
    "float16": np.dtype(np.float16),
    "int8":    np.dtype(np.int8),
    "uint8":   np.dtype(np.uint8),
    "int16":   np.dtype(np.int16),
    "uint16":  np.dtype(np.uint16),
    "int32":   np.dtype(np.int32),
    "int64":   np.dtype(np.int64),
}

_DTYPE_REVERSE: dict[np.dtype, str] = {v: k for k, v in _DTYPE_MAP.items()}


def _normalize_dtype_name(raw: str) -> str:
    """Accept synonyms returned by qai_appbuilder native getter (e.g. 'fp32').

    Falls back to lowercasing the input. Unknown dtype strings are passed
    through; downstream :func:`validate_inputs` will surface the mismatch.
    """
    s = (raw or "").strip().lower()
    return {
        "fp32": "float32", "f32": "float32",
        "fp16": "float16", "f16": "float16",
        "i8": "int8", "u8": "uint8",
        "i16": "int16", "u16": "uint16",
        "i32": "int32", "i64": "int64",
    }.get(s, s)


# ──────────────────────────────────────────────────────────────────────────
# Layout heuristic (4-D image tensors only; otherwise "raw")
# ──────────────────────────────────────────────────────────────────────────
def _infer_layout(shape: list[int | None]) -> str:
    """Heuristically guess image layout (NCHW vs NHWC) from a shape vector.

    Used purely as advisory metadata in the contract; runners cross-check
    against the native API rather than trusting this value.
    """
    if not shape or len(shape) != 4:
        return "raw"
    _, a, b, c = shape
    # Channels-typical small set: 1/3/4. The dimension that matches AND
    # whose siblings are spatial-typical (>= 8) wins.
    if a in (1, 3, 4) and (b is None or b >= 8) and (c is None or c >= 8):
        return "NCHW"
    if c in (1, 3, 4) and (a is None or a >= 8) and (b is None or b >= 8):
        return "NHWC"
    return "raw"


# ──────────────────────────────────────────────────────────────────────────
# Contract extraction from a live qai_appbuilder.QNNContext
# ──────────────────────────────────────────────────────────────────────────
def extract_io_contract(qnn_ctx: Any, *, validated_at_export: bool = False) -> dict:
    """Query a live ``qai_appbuilder.QNNContext`` for its full I/O contract.

    Parameters
    ----------
    qnn_ctx : qai_appbuilder.QNNContext  (or anything implementing the same
              ``getInputShapes / getOutputShapes / getInputDataType /
              getOutputDataType / getInputName / getOutputName / getGraphName``
              method set).

    Returns
    -------
    contract : dict
        Fully populated contract with ``schema_version=1``. Caller can serialize
        directly into ``manifest.json["io_contract"]``.

    Raises
    ------
    IOContractError(code='CONTRACT_MISSING')
        when the underlying native API throws or returns mismatched lengths.
    """
    try:
        in_shapes  = list(qnn_ctx.getInputShapes())
        out_shapes = list(qnn_ctx.getOutputShapes())
        in_dtypes  = [_normalize_dtype_name(s) for s in qnn_ctx.getInputDataType()]
        out_dtypes = [_normalize_dtype_name(s) for s in qnn_ctx.getOutputDataType()]
        in_names   = list(qnn_ctx.getInputName())
        out_names  = list(qnn_ctx.getOutputName())
        graph_name = str(qnn_ctx.getGraphName() or "unknown")
    except Exception as exc:  # noqa: BLE001 — the native API can raise anything
        raise IOContractError(
            "CONTRACT_MISSING",
            f"Failed to query qai_appbuilder native shape APIs: {exc!r}",
            {"exception": repr(exc)},
        ) from exc

    if len(in_shapes) != len(in_dtypes) or len(in_shapes) != len(in_names):
        raise IOContractError(
            "CONTRACT_MISSING",
            "qai_appbuilder returned inconsistent input metadata "
            f"(shapes={len(in_shapes)}, dtypes={len(in_dtypes)}, names={len(in_names)})",
            {"in_shapes": in_shapes, "in_dtypes": in_dtypes, "in_names": in_names},
        )
    if len(out_shapes) != len(out_dtypes) or len(out_shapes) != len(out_names):
        raise IOContractError(
            "CONTRACT_MISSING",
            "qai_appbuilder returned inconsistent output metadata",
            {"out_shapes": out_shapes, "out_dtypes": out_dtypes, "out_names": out_names},
        )

    def _spec(name: str, shape: list, dtype: str) -> dict:
        spec: dict[str, Any] = {
            "name":   str(name),
            "dtype":  dtype,
            "shape":  [int(d) if d is not None and int(d) > 0 else None for d in shape],
            "layout": _infer_layout(list(shape)),
        }
        # Only fully-static shapes get a byte_size sanity field.
        if all(d is not None and d > 0 for d in spec["shape"]) and dtype in _DTYPE_MAP:
            n = 1
            for d in spec["shape"]:
                n *= d
            spec["byte_size"] = int(n) * int(_DTYPE_MAP[dtype].itemsize)
        return spec

    contract: dict[str, Any] = {
        "schema_version": 1,
        "graph_name":     graph_name,
        "inputs":         [_spec(n, s, d) for n, s, d in zip(in_names,  in_shapes,  in_dtypes)],
        "outputs":        [_spec(n, s, d) for n, s, d in zip(out_names, out_shapes, out_dtypes)],
        "validated_at_export": bool(validated_at_export),
    }
    return contract


# ──────────────────────────────────────────────────────────────────────────
# Static cross-check between the manifest (export-time) and live (load-time)
# ──────────────────────────────────────────────────────────────────────────
def assert_contracts_compatible(static: dict | None, live: dict) -> None:
    """Verify the manifest contract still describes the loaded .bin.

    A mismatch signals one of:
      - a stale manifest after the .bin was rebuilt
      - the .bin was hand-replaced post-import
      - the Pack contains an entirely different model than its manifest
        claims
    All three are operator errors, not user errors; we surface a typed
    error rather than letting native code crash later.

    ``static`` is allowed to be None (legacy Pack predating io_contract);
    we then fall back to live-only enforcement and log a warning upstream.
    """
    if not static:
        return  # caller decides whether to warn-or-block; we don't enforce here

    def _shapes(c: dict, key: str) -> list:
        return [list(t.get("shape") or []) for t in c.get(key, [])]

    def _dtypes(c: dict, key: str) -> list:
        return [t.get("dtype") for t in c.get(key, [])]

    if _shapes(static, "inputs") != _shapes(live, "inputs"):
        raise IOContractError(
            "CONTRACT_MISMATCH",
            "manifest.io_contract.inputs.shape != live model getInputShapes()",
            {"static": _shapes(static, "inputs"), "live": _shapes(live, "inputs")},
        )
    if _dtypes(static, "inputs") != _dtypes(live, "inputs"):
        raise IOContractError(
            "CONTRACT_MISMATCH",
            "manifest.io_contract.inputs.dtype != live model getInputDataType()",
            {"static": _dtypes(static, "inputs"), "live": _dtypes(live, "inputs")},
        )
    # Output shape diff is a warning condition for some models that emit
    # post-process variable-length outputs; we still raise to be strict — a
    # Pack with diverging output dtype is broken.
    if _dtypes(static, "outputs") != _dtypes(live, "outputs"):
        raise IOContractError(
            "CONTRACT_MISMATCH",
            "manifest.io_contract.outputs.dtype != live model getOutputDataType()",
            {"static": _dtypes(static, "outputs"), "live": _dtypes(live, "outputs")},
        )


# ──────────────────────────────────────────────────────────────────────────
# Single-tensor validation / coercion
# ──────────────────────────────────────────────────────────────────────────
def prepare_input_for_contract(
    arr: np.ndarray,
    spec: dict,
    *,
    name_hint: str = "input",
) -> np.ndarray:
    """Validate one tensor against one entry of contract.inputs, coercing
    where it is *safe* (dtype upcast, contiguous copy) and raising
    otherwise.

    Coercions performed silently
    ----------------------------
    * float64 / float16 → float32  (when contract dtype is float32 and the
      cast preserves all values within ``same_kind`` semantics)
    * non-contiguous view → contiguous copy
    * batch axis size of 1 added when the contract is 4-D and the caller
      passed a 3-D image (commonly forgotten in scripts)

    Errors raised (never silently fixed)
    ------------------------------------
    * INVALID_INPUT_DTYPE — incompatible-kind cast (e.g. int8 → float32)
    * INVALID_INPUT_RANK  — wrong number of dimensions and not "missing batch"
    * INVALID_INPUT_SHAPE — static-axis mismatch
    * INVALID_INPUT_BYTES — final nbytes != contract byte_size
    """
    expected_shape: list[int | None] = list(spec.get("shape") or [])
    expected_dtype_name = _normalize_dtype_name(spec.get("dtype") or "")
    expected_dtype = _DTYPE_MAP.get(expected_dtype_name)
    name = spec.get("name") or name_hint

    if expected_dtype is None:
        # Unknown dtype in contract — surface clearly rather than crash.
        raise IOContractError(
            "INVALID_INPUT_DTYPE",
            f"{name}: contract specifies unsupported dtype {spec.get('dtype')!r}",
            {"contract_dtype": spec.get("dtype")},
        )

    # ── 1) dtype: upcast if safe, error otherwise ─────────────────────────
    if arr.dtype != expected_dtype:
        if np.can_cast(arr.dtype, expected_dtype, casting="same_kind"):
            arr = arr.astype(expected_dtype, copy=True)
        else:
            raise IOContractError(
                "INVALID_INPUT_DTYPE",
                f"{name}: dtype {arr.dtype} cannot be safely cast to {expected_dtype_name}",
                {"got_dtype": str(arr.dtype), "expected_dtype": expected_dtype_name},
            )

    # ── 2) rank: allow common "missing batch" auto-fix ────────────────────
    if arr.ndim != len(expected_shape):
        # Auto-add a leading batch axis if we can.
        if (
            arr.ndim == len(expected_shape) - 1
            and expected_shape
            and (expected_shape[0] in (1, None))
        ):
            arr = arr[np.newaxis, ...]
        else:
            raise IOContractError(
                "INVALID_INPUT_RANK",
                f"{name}: expected rank {len(expected_shape)}, got {arr.ndim}",
                {"got_shape": list(arr.shape), "expected_shape": expected_shape},
            )

    # ── 3) per-axis size ──────────────────────────────────────────────────
    for axis, (got, want) in enumerate(zip(arr.shape, expected_shape)):
        if want is None:
            continue  # dynamic axis
        if got != want:
            raise IOContractError(
                "INVALID_INPUT_SHAPE",
                f"{name}: axis {axis} expected {want}, got {got}",
                {"axis": axis, "got_shape": list(arr.shape), "expected_shape": expected_shape},
            )

    # ── 4) C-contiguous + byte sanity (last line before native memcpy) ───
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)

    declared_bytes = spec.get("byte_size")
    if declared_bytes is not None:
        # byte_size is only set on fully-static contracts; if we got here
        # past per-axis checks the math should agree, but defense in depth
        # is cheap.
        if arr.nbytes != declared_bytes:
            raise IOContractError(
                "INVALID_INPUT_BYTES",
                f"{name}: nbytes {arr.nbytes} != contract byte_size {declared_bytes}",
                {
                    "got_bytes": int(arr.nbytes),
                    "expected_bytes": int(declared_bytes),
                    "got_shape": list(arr.shape),
                    "expected_shape": expected_shape,
                },
            )

    return arr


def validate_inputs(arrays: Iterable[np.ndarray], contract: dict) -> list[np.ndarray]:
    """Validate every tensor in ``arrays`` against ``contract.inputs``.

    Returns a list of arrays (possibly reallocated for contiguity / dtype)
    that is guaranteed safe to pass to ``ctx.run``.

    Raises :class:`IOContractError` on the first violation. Never returns
    a partially-coerced batch.
    """
    arrays = list(arrays)
    inputs_meta = contract.get("inputs") or []
    if len(arrays) != len(inputs_meta):
        raise IOContractError(
            "INVALID_INPUT_COUNT",
            f"expected {len(inputs_meta)} input tensors, got {len(arrays)}",
            {"got_count": len(arrays), "expected_count": len(inputs_meta)},
        )
    return [
        prepare_input_for_contract(a, m, name_hint=f"input{i}")
        for i, (a, m) in enumerate(zip(arrays, inputs_meta))
    ]


# ──────────────────────────────────────────────────────────────────────────
# Convenience: a zero-tensor batch matching a contract (used by smoke tests)
# ──────────────────────────────────────────────────────────────────────────
def zero_inputs_for_contract(contract: dict, *, batch_for_dynamic: int = 1) -> list[np.ndarray]:
    """Build a list of zero-filled tensors that satisfies ``contract.inputs``.

    Used by the export-side and import-side smoke tests to verify the model
    can actually be invoked. Dynamic axes are materialised at
    ``batch_for_dynamic`` (default 1).
    """
    out: list[np.ndarray] = []
    for spec in contract.get("inputs") or []:
        dtype = _DTYPE_MAP.get(_normalize_dtype_name(spec.get("dtype", "")))
        if dtype is None:
            raise IOContractError(
                "INVALID_INPUT_DTYPE",
                f"cannot synthesize zero tensor for unknown dtype {spec.get('dtype')!r}",
                {"contract_dtype": spec.get("dtype")},
            )
        shape = [d if d is not None and d > 0 else batch_for_dynamic for d in spec.get("shape") or []]
        out.append(np.zeros(shape, dtype=dtype))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Convenience: extract input H/W from a CV contract (NCHW or NHWC)
# ──────────────────────────────────────────────────────────────────────────
def cv_input_hw(contract: dict, *, input_index: int = 0) -> tuple[int, int]:
    """Return (H, W) of the indexed image input.

    Raises IOContractError if the contract isn't 4-D image-shaped.
    """
    inputs = contract.get("inputs") or []
    if input_index >= len(inputs):
        raise IOContractError(
            "INVALID_INPUT_COUNT",
            f"contract has {len(inputs)} inputs, index {input_index} out of range",
            {"input_index": input_index, "input_count": len(inputs)},
        )
    spec = inputs[input_index]
    shape = spec.get("shape") or []
    if len(shape) != 4:
        raise IOContractError(
            "INVALID_INPUT_RANK",
            f"input {input_index} is rank {len(shape)}, expected 4-D image tensor",
            {"shape": shape},
        )
    layout = spec.get("layout") or _infer_layout(shape)
    if layout == "NCHW":
        h, w = shape[2], shape[3]
    elif layout == "NHWC":
        h, w = shape[1], shape[2]
    else:
        # As a last resort assume NCHW (the more common layout for QNN exports)
        h, w = shape[2], shape[3]
    if h is None or w is None:
        raise IOContractError(
            "INVALID_INPUT_SHAPE",
            f"input {input_index} has dynamic spatial dimensions; preprocessing requires concrete H/W",
            {"shape": shape, "layout": layout},
        )
    return int(h), int(w)


# ──────────────────────────────────────────────────────────────────────────
# JSON dump helper (stable formatting for diffs)
# ──────────────────────────────────────────────────────────────────────────
def to_json(contract: dict) -> str:
    """Pretty-print a contract for embedding in manifest.json or logs."""
    return json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=False)
