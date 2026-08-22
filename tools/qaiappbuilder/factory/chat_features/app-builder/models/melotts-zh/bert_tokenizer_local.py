# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Local BERT WordPiece tokenizer using bert_zh_tokenizer.bin + bert_normalizer.bin.

Replaces the `transformers.AutoTokenizer` dependency for bert-base-multilingual-uncased.

Binary formats:
  - bert_zh_tokenizer.bin: vocab + cache indices + SingleTokenInfo arrays
  - bert_normalizer.bin: Unicode DAG for NFKD decomposition + accent stripping + lowercase

Author: Auto-generated for MeloTTS-ZH Full-NPU pipeline.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

# =====================================================================
# Special token IDs (bert-base-multilingual-uncased)
# =====================================================================
PAD_ID = 0
UNK_ID = 100
CLS_ID = 101
SEP_ID = 102
MASK_ID = 103


# =====================================================================
# Normalizer: parse bert_normalizer.bin (Unicode DAG)
# =====================================================================

def load_normalizer(bin_path: str | Path) -> dict[int, list[int]]:
    """Parse bert_normalizer.bin and return codepoint -> decomposed codepoints mapping.

    The binary contains a BFS-ordered DAG where:
      - Header: num_entries (uint32), size_data_fixed (uint32), size_data_variable (uint32)
      - Fixed section: num_entries * node structs (int unicode_value, char[3] gc, uint32 num_children, uint32 decomp_offset)
      - Variable section: child indices (uint32 arrays)

    We reconstruct the DAG and produce a mapping: codepoint -> list of base codepoints
    (after NFKD decomposition with Mn-category chars stripped).
    """
    bin_path = Path(bin_path)
    with open(bin_path, "rb") as f:
        data = f.read()

    # Header
    num_entries, size_data_fixed, size_data_variable = struct.unpack_from("III", data, 0)
    header_size = 12

    # Parse fixed section: each node is struct { int unicode_value; char gc[3]; uint32 num_children; uint32 decomp_offset; }
    # Format: "i 3s I I" → 4 + 3 + (1 padding) + 4 + 4 = 16 bytes
    node_size = struct.calcsize("i 3s I I")  # Should be 16

    nodes = []  # list of (unicode_value, gc_str, num_children, decomp_offset)
    for i in range(num_entries):
        offset = header_size + i * node_size
        uc_val, gc_bytes, num_children, decomp_offset = struct.unpack_from(
            "i 3s I I", data, offset
        )
        gc_str = gc_bytes.decode("ascii", errors="ignore").rstrip("\x00")
        nodes.append((uc_val, gc_str, num_children, decomp_offset))

    # Variable section starts after fixed section
    var_section_offset = header_size + size_data_fixed

    # Build the decomposition map by traversing the DAG
    # Root node (index 0) has unicode_value == -1, its children are the top-level entries
    # For each top-level entry, recursively find leaf nodes (nodes with 0 children)
    # that are NOT in the "Mn" (Mark, Nonspacing) category

    def get_children_indices(node_idx: int) -> list[int]:
        """Get child node indices from variable section."""
        _, _, num_children, decomp_offset = nodes[node_idx]
        if num_children == 0:
            return []
        children = []
        for j in range(num_children):
            child_offset = var_section_offset + decomp_offset + j * 4
            child_idx = struct.unpack_from("I", data, child_offset)[0]
            children.append(child_idx)
        return children

    def get_leaf_codepoints(node_idx: int) -> list[int]:
        """Recursively get non-Mn leaf codepoints from a node."""
        uc_val, gc, num_children, _ = nodes[node_idx]
        if num_children == 0:
            # Leaf node: include if not Mn category
            if gc == "Mn":
                return []
            return [uc_val]
        # Non-leaf: recurse into children
        result = []
        for child_idx in get_children_indices(node_idx):
            result.extend(get_leaf_codepoints(child_idx))
        return result

    # Build mapping: for each child of root, map its unicode_value to decomposed codepoints
    normalizer_map: dict[int, list[int]] = {}

    root_children = get_children_indices(0)
    for child_idx in root_children:
        uc_val, gc, num_children, _ = nodes[child_idx]
        if num_children == 0:
            # No decomposition needed for this codepoint
            # But we still want to store it (identity mapping not needed unless Mn)
            if gc != "Mn":
                normalizer_map[uc_val] = [uc_val]
            else:
                normalizer_map[uc_val] = []  # Strip Mn chars
        else:
            # Has decomposition
            decomposed = get_leaf_codepoints(child_idx)
            normalizer_map[uc_val] = decomposed

    return normalizer_map


def normalize_text(text: str, normalizer_map: dict[int, list[int]]) -> str:
    """Normalize text: NFKD decomposition + accent stripping + lowercase.

    For bert-base-multilingual-uncased:
      1. NFKD decompose (using normalizer_map)
      2. Strip combining marks (Mn category) - already done in decomposition
      3. Lowercase

    For Chinese characters, this is effectively just lowercase (no-op for CJK).
    """
    result = []
    for ch in text:
        cp = ord(ch)
        if cp in normalizer_map:
            # Use pre-computed decomposition (Mn already stripped)
            for dcp in normalizer_map[cp]:
                result.append(chr(dcp))
        else:
            # Character not in normalizer DAG, keep as-is
            result.append(ch)

    # Lowercase the result
    return "".join(result).lower()


# =====================================================================
# Tokenizer: parse bert_zh_tokenizer.bin
# =====================================================================

def load_tokenizer(bin_path: str | Path) -> dict:
    """Parse bert_zh_tokenizer.bin and return tokenizer data.

    Binary format:
      Header (6 x uint32):
        numFulls, numCacheLineIndicesFulls, numPartials, numCacheLineIndicesPartials,
        sizeVocabFull, sizeVocabPartial

      Body:
        char* pVocabFull (padded to 256-byte alignment)
        uint32_t* pCacheLineIndicesFulls
        char* pVocabPartials (padded to 256-byte alignment)
        uint32_t* pCacheLineIndicesPartials
        SingleTokenInfo[numFulls] pFulls
        SingleTokenInfo[numPartials] pPartials

    SingleTokenInfo: vocabOffset(uint32), tokenIdx(uint32), numChars(int32), numBytes(int32)

    Returns dict with keys:
      - vocab_fulls: dict[str, int] mapping token string to token_id
      - vocab_partials: dict[str, int] mapping "##xxx" to token_id
    """
    bin_path = Path(bin_path)
    with open(bin_path, "rb") as f:
        data = f.read()

    # Parse header
    (num_fulls, num_cache_fulls, num_partials, num_cache_partials,
     size_vocab_full, size_vocab_partial) = struct.unpack_from("6I", data, 0)

    header_size = 6 * 4  # 24 bytes
    offset = header_size

    # pVocabFull (size_vocab_full bytes, already includes padding)
    vocab_full_bytes = data[offset: offset + size_vocab_full]
    offset += size_vocab_full

    # pCacheLineIndicesFulls (num_cache_fulls * uint32)
    cache_fulls_size = num_cache_fulls * 4
    offset += cache_fulls_size

    # pVocabPartials (size_vocab_partial bytes, already includes padding)
    vocab_partial_bytes = data[offset: offset + size_vocab_partial]
    offset += size_vocab_partial

    # pCacheLineIndicesPartials (num_cache_partials * uint32)
    cache_partials_size = num_cache_partials * 4
    offset += cache_partials_size

    # SingleTokenInfo[numFulls] pFulls
    # Each: vocabOffset(uint32), tokenIdx(uint32), numChars(int32), numBytes(int32) = 16 bytes
    vocab_fulls: dict[str, int] = {}
    for i in range(num_fulls):
        vocab_offset, token_idx, num_chars, num_bytes = struct.unpack_from(
            "IIii", data, offset + i * 16
        )
        # Extract token string from vocab_full_bytes
        # num_bytes includes the null terminator
        token_bytes = vocab_full_bytes[vocab_offset: vocab_offset + num_bytes - 1]
        token_str = token_bytes.decode("utf-8", errors="replace")
        vocab_fulls[token_str] = token_idx
    offset += num_fulls * 16

    # SingleTokenInfo[numPartials] pPartials
    vocab_partials: dict[str, int] = {}
    for i in range(num_partials):
        vocab_offset, token_idx, num_chars, num_bytes = struct.unpack_from(
            "IIii", data, offset + i * 16
        )
        # Extract token string from vocab_partial_bytes
        token_bytes = vocab_partial_bytes[vocab_offset: vocab_offset + num_bytes - 1]
        token_str = token_bytes.decode("utf-8", errors="replace")
        vocab_partials[token_str] = token_idx
    offset += num_partials * 16

    return {
        "vocab_fulls": vocab_fulls,
        "vocab_partials": vocab_partials,
    }


# =====================================================================
# WordPiece tokenization
# =====================================================================

def _is_chinese_char(cp: int) -> bool:
    """Check if a codepoint is a CJK character."""
    return (
        (0x4E00 <= cp <= 0x9FFF)
        or (0x3400 <= cp <= 0x4DBF)
        or (0x20000 <= cp <= 0x2A6DF)
        or (0x2A700 <= cp <= 0x2B73F)
        or (0x2B740 <= cp <= 0x2B81F)
        or (0x2B820 <= cp <= 0x2CEAF)
        or (0xF900 <= cp <= 0xFAFF)
        or (0x2F800 <= cp <= 0x2FA1F)
    )


def _is_whitespace(ch: str) -> bool:
    """Check if character is whitespace."""
    if ch in (" ", "\t", "\n", "\r"):
        return True
    import unicodedata
    cat = unicodedata.category(ch)
    if cat == "Zs":
        return True
    return False


def _is_punctuation(ch: str) -> bool:
    """Check if character is punctuation."""
    cp = ord(ch)
    # ASCII punctuation
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    import unicodedata
    cat = unicodedata.category(ch)
    if cat.startswith("P"):
        return True
    return False


def _tokenize_chinese_chars(text: str) -> str:
    """Add spaces around CJK characters (BERT BasicTokenizer behavior)."""
    output = []
    for ch in text:
        cp = ord(ch)
        if _is_chinese_char(cp):
            output.append(" ")
            output.append(ch)
            output.append(" ")
        else:
            output.append(ch)
    return "".join(output)


def _basic_tokenize(text: str) -> list[str]:
    """BERT BasicTokenizer: whitespace + punctuation splitting.

    For multilingual-uncased, text is already lowercased and accent-stripped.
    """
    # Add spaces around CJK chars
    text = _tokenize_chinese_chars(text)

    # Split on whitespace
    orig_tokens = text.strip().split()

    # Split each token on punctuation
    output_tokens = []
    for token in orig_tokens:
        chars = list(token)
        i = 0
        start_new_word = True
        current_word = []
        for ch in chars:
            if _is_punctuation(ch):
                if current_word:
                    output_tokens.append("".join(current_word))
                    current_word = []
                output_tokens.append(ch)
                start_new_word = True
            else:
                current_word.append(ch)
                start_new_word = False
        if current_word:
            output_tokens.append("".join(current_word))

    return output_tokens


def _wordpiece_tokenize(
    word: str, vocab_fulls: dict[str, int], vocab_partials: dict[str, int],
    max_word_chars: int = 200
) -> list[str]:
    """WordPiece tokenization for a single word."""
    if len(word) > max_word_chars:
        return ["[UNK]"]

    tokens = []
    start = 0
    while start < len(word):
        end = len(word)
        cur_substr = None
        while start < end:
            substr = word[start:end]
            if start > 0:
                lookup = "##" + substr
                if lookup in vocab_partials:
                    cur_substr = lookup
                    break
            else:
                if substr in vocab_fulls:
                    cur_substr = substr
                    break
            end -= 1
        if cur_substr is None:
            tokens.append("[UNK]")
            start += 1
        else:
            tokens.append(cur_substr)
            start = end
    return tokens


# =====================================================================
# Main tokenize function
# =====================================================================

def tokenize(
    text: str,
    tokenizer_data: dict,
    normalizer_map: dict[int, list[int]],
    max_length: int = 200,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Full BERT tokenize pipeline (bert-base-multilingual-uncased compatible).

    Steps:
      1. Normalize text (NFKD + strip accents + lowercase)
      2. Basic tokenization (whitespace + CJK spacing + punctuation split)
      3. WordPiece sub-tokenization
      4. Add [CLS] / [SEP]
      5. Pad to max_length

    Args:
        text: Input text string
        tokenizer_data: Output of load_tokenizer()
        normalizer_map: Output of load_normalizer()
        max_length: Pad/truncate to this length (default 200)

    Returns:
        (input_ids, token_type_ids, attention_mask) - all int32 numpy arrays of shape [1, max_length]
    """
    vocab_fulls = tokenizer_data["vocab_fulls"]
    vocab_partials = tokenizer_data["vocab_partials"]

    # 1. Normalize
    normalized = normalize_text(text, normalizer_map)

    # 2. Basic tokenize
    basic_tokens = _basic_tokenize(normalized)

    # 3. WordPiece
    all_tokens = []
    for word in basic_tokens:
        wp_tokens = _wordpiece_tokenize(word, vocab_fulls, vocab_partials)
        all_tokens.extend(wp_tokens)

    # 4. Truncate (account for [CLS] and [SEP])
    if len(all_tokens) > max_length - 2:
        all_tokens = all_tokens[: max_length - 2]

    # 5. Convert to IDs
    token_ids = [CLS_ID]
    for t in all_tokens:
        if t == "[UNK]":
            token_ids.append(UNK_ID)
        elif t.startswith("##"):
            token_ids.append(vocab_partials.get(t, UNK_ID))
        else:
            token_ids.append(vocab_fulls.get(t, UNK_ID))
    token_ids.append(SEP_ID)

    # 6. Pad
    attention_len = len(token_ids)
    padding_len = max_length - attention_len
    if padding_len > 0:
        token_ids.extend([PAD_ID] * padding_len)

    # Build outputs
    input_ids = np.array(token_ids[:max_length], dtype=np.int32).reshape(1, max_length)
    token_type_ids = np.zeros((1, max_length), dtype=np.int32)
    attention_mask = np.zeros((1, max_length), dtype=np.int32)
    attention_mask[0, :attention_len] = 1

    return input_ids, token_type_ids, attention_mask


# =====================================================================
# Convenience: all-in-one loader
# =====================================================================

class BertTokenizerLocal:
    """Drop-in replacement for transformers AutoTokenizer (subset API).

    Usage:
        tokenizer = BertTokenizerLocal(tokenizer_bin, normalizer_bin)
        result = tokenizer(text, padding="max_length", max_length=200,
                          truncation=True, return_tensors="np")
        input_ids = result["input_ids"]       # [1, 200] int32
        token_type_ids = result["token_type_ids"]  # [1, 200] int32
        attention_mask = result["attention_mask"]   # [1, 200] int32
    """

    def __init__(self, tokenizer_bin: str | Path, normalizer_bin: str | Path):
        self.tokenizer_data = load_tokenizer(tokenizer_bin)
        self.normalizer_map = load_normalizer(normalizer_bin)

    def __call__(
        self,
        text: str,
        padding: str = "max_length",
        max_length: int = 200,
        truncation: bool = True,
        return_tensors: str = "np",
        **kwargs,
    ) -> dict[str, np.ndarray]:
        input_ids, token_type_ids, attention_mask = tokenize(
            text, self.tokenizer_data, self.normalizer_map, max_length=max_length
        )
        return {
            "input_ids": input_ids,
            "token_type_ids": token_type_ids,
            "attention_mask": attention_mask,
        }


# =====================================================================
# Self-test
# =====================================================================

if __name__ == "__main__":
    import sys

    # Default paths
    this_dir = Path(__file__).resolve().parent
    model_dir = (
        this_dir / "python" / "models"
        / "melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite"
    )
    tok_bin = model_dir / "bert_zh_tokenizer.bin"
    norm_bin = model_dir / "bert_normalizer.bin"

    if not tok_bin.exists():
        print(f"ERROR: {tok_bin} not found")
        sys.exit(1)
    if not norm_bin.exists():
        print(f"ERROR: {norm_bin} not found")
        sys.exit(1)

    print("Loading normalizer ...")
    norm_map = load_normalizer(norm_bin)
    print(f"  Normalizer entries: {len(norm_map)}")

    print("Loading tokenizer ...")
    tok_data = load_tokenizer(tok_bin)
    print(f"  Full vocab: {len(tok_data['vocab_fulls'])} tokens")
    print(f"  Partial vocab: {len(tok_data['vocab_partials'])} tokens")

    # Test tokenization
    test_text = "中文是中国的语言文字"
    print(f"\nTokenizing: '{test_text}'")
    input_ids, token_type_ids, attention_mask = tokenize(
        test_text, tok_data, norm_map, max_length=200
    )
    print(f"  input_ids[:15]: {input_ids[0, :15].tolist()}")
    print(f"  attention_mask sum: {attention_mask.sum()}")

    # Expected (bert-base-multilingual-uncased):
    # [101, 1683, 4278, 4353, 1683, 2751, 5975, 8007, 7785, 4278, 3160, 102, 0, ...]
    expected_start = [101, 1683, 4278, 4353, 1683, 2751, 5975, 8007, 7785, 4278, 3160, 102]
    actual_start = input_ids[0, :12].tolist()
    if actual_start == expected_start:
        print("\n  ✓ PASS: Output matches expected transformers AutoTokenizer result!")
    else:
        print(f"\n  ✗ FAIL: Expected {expected_start}")
        print(f"           Got      {actual_start}")
        sys.exit(1)
