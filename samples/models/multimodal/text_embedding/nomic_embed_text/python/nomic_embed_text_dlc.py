#!/usr/bin/env python3
"""Run a Nomic text-embedding DLC through QAI AppBuilder.

DLC Model Download URL:
    https://aihub.qualcomm.com/iot/models/nomic_embed_text

Examples:
    python nomic_embed_text_dlc.py \
        --model nomic_embed_text.dlc \
        --tokenizer tokenizer_path
    python nomic_embed_text_dlc.py --model nomic_embed_text.dlc
    python nomic_embed_text_dlc.py --model model.dlc --text "hello"
    python nomic_embed_text_dlc.py --model model.dlc --text "a" \
        --cosine-text "b"

The DLC must have two inputs (input_ids and attention_mask), or expose them in
that order.  ``transformers`` is required for tokenization.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np


def _metadata(context: Any, method: str) -> Any:
    """Read optional QNNContext metadata without making old SDKs unusable."""
    value = getattr(context, method, None)
    if value is None:
        return "<unavailable>"
    try:
        return value() if callable(value) else value
    except Exception as exc:  # metadata is diagnostic only
        return f"<unavailable: {exc}>"


def _print_metadata(context: Any, model: Path) -> None:
    print(f"[INFO] DLC: {model}")
    print(f"[INFO] Graph: {_metadata(context, 'getGraphName')}")
    print(f"[INFO] Inputs: {_metadata(context, 'getInputName')}")
    print(f"[INFO] Input shapes: {_metadata(context, 'getInputShapes')}")
    print(f"[INFO] Input dtypes: {_metadata(context, 'getInputDataType')}")
    print(f"[INFO] Outputs: {_metadata(context, 'getOutputName')}")
    print(f"[INFO] Output shapes: {_metadata(context, 'getOutputShapes')}")
    print(f"[INFO] Output dtypes: {_metadata(context, 'getOutputDataType')}")


def _load_tokenizer(name: str, seq_len: int) -> Any:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "Missing 'transformers' package. Please install it first: pip install transformers"
        ) from exc
    try:
        tokenizer = AutoTokenizer.from_pretrained(name)
    except Exception as exc:
        raise RuntimeError(f"Failed to load tokenizer '{name}': {exc}") from exc
    if tokenizer.pad_token_id is None:
        if tokenizer.pad_token is None:
            # Fallback for common pretrained models (BERT, Nomic, etc.)
            if "[PAD]" in tokenizer.get_vocab():
                tokenizer.pad_token = "[PAD]"
            elif "<pad>" in tokenizer.get_vocab():
                tokenizer.pad_token = "<pad>"
            elif tokenizer.eos_token is not None:
                tokenizer.pad_token = tokenizer.eos_token
            else:
                tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    return tokenizer


def _tokenize(tokenizer: Any, text: str, seq_len: int) -> tuple[np.ndarray, np.ndarray]:
    encoded = tokenizer(
        text,
        padding="max_length",
        truncation=True,
        max_length=seq_len,
        return_tensors="np",
    )
    try:
        input_ids = np.asarray(encoded["input_ids"], dtype=np.int32)
        attention_mask = np.asarray(encoded["attention_mask"], dtype=np.int32)
    except KeyError as exc:
        raise RuntimeError(f"Tokenizer output missing required key: {exc.args[0]}") from exc
    return input_ids, attention_mask


def _ordered_inputs(context: Any, input_ids: np.ndarray, mask: np.ndarray) -> list[np.ndarray]:
    names = _metadata(context, "getInputName")
    if isinstance(names, (list, tuple)) and len(names) == 2:
        lowered = [str(name).lower() for name in names]
        if any("mask" in name or "attention" in name for name in lowered):
            result: list[np.ndarray] = []
            for name in lowered:
                result.append(mask if ("mask" in name or "attention" in name) else input_ids)
            return result
    # Existing Nomic conversion emits input_ids followed by attention_mask.
    return [input_ids, mask]


def _infer(context: Any, tokenizer: Any, text: str, seq_len: int) -> np.ndarray:
    input_ids, attention_mask = _tokenize(tokenizer, text, seq_len)
    outputs = context.Inference(_ordered_inputs(context, input_ids, attention_mask))
    if not outputs:
        raise RuntimeError("DLC did not return any output")
    embedding = np.asarray(outputs[0])
    print(f"[INFO] Embedding shape: {embedding.shape}")
    print(f"[INFO] Embedding preview: {embedding.reshape(-1)[:12]}")
    return embedding


def _cosine(first: np.ndarray, second: np.ndarray) -> float:
    a, b = first.astype(np.float64).reshape(-1), second.astype(np.float64).reshape(-1)
    if a.size != b.size:
        raise ValueError(f"Embedding shape mismatch: {a.size} vs {b.size} elements")
    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denominator) if denominator else 0.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run text embedding DLC on QAI AppBuilder HTP backend")
    parser.add_argument("--model", required=True, help="Path to the DLC file")
    parser.add_argument("--text", help="Single text to encode. If not provided, enters interactive CLI mode.")
    parser.add_argument("--tokenizer", default="bert-base-uncased", help="Transformers tokenizer name or local path (default: bert-base-uncased)")
    parser.add_argument("--seq-len", type=int, default=128, help="Fixed sequence length (default: 128)")
    parser.add_argument("--cosine-text", help="Text to compute cosine similarity against --text")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.seq_len <= 0:
        print("[ERROR] --seq-len must be a positive integer", file=sys.stderr)
        return 2
    model = Path(args.model).expanduser()
    if not model.is_file():
        print(f"[ERROR] DLC file does not exist or is not readable: {model}", file=sys.stderr)
        return 2

    context: Optional[Any] = None
    try:
        from qai_appbuilder import QNNConfig, QNNContext, Runtime, LogLevel, ProfilingLevel
    except ImportError as exc:
        print("[ERROR] Failed to import qai_appbuilder. Please run: pip install qai-appbuilder", file=sys.stderr)
        print(f"        Details: {exc}", file=sys.stderr)
        return 1

    try:
        tokenizer = _load_tokenizer(args.tokenizer, args.seq_len)
        print("[INFO] Runtime: HTP")
        QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
        context = QNNContext("nomic_embed_text", str(model))
        _print_metadata(context, model)

        def run(text: str) -> np.ndarray:
            if not text.strip():
                raise ValueError("Input text cannot be empty")
            return _infer(context, tokenizer, text, args.seq_len)

        if args.text is not None:
            first = run(args.text)
            if args.cosine_text is not None:
                print(f"[INFO] Cosine similarity: {_cosine(first, run(args.cosine_text)):.8f}")
        else:
            print("[INFO] Interactive mode: enter text and press Enter. Type 'exit' or press Ctrl-D to quit.")
            while True:
                try:
                    text = input("text> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if text.strip().lower() in {"exit", "quit"}:
                    break
                try:
                    run(text)
                except Exception as exc:
                    print(f"[ERROR] Inference failed: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        # QNNContext owns native resources; deleting it is the SDK-supported cleanup.
        if context is not None:
            try:
                del context
            except Exception as exc:
                print(f"[WARN] Failed to release QNNContext: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
