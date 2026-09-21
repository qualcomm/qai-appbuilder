# Transformer Decoder ONNX/QNN Prefill + Decode Porting

Use this reference when modifying model export, ONNX graph patching, QNN/QAIRT conversion, and inference scripts for a PyTorch/HuggingFace transformer decoder model targeting Qualcomm HTP/NPU.

This reference focuses on the ONNX-facing contract and runtime work. For PyTorch/HuggingFace model-side wrapper guidance, see `pytorch_modification.md`.

---

## Background: KV Cache and NPU Exposure

### What is KV Cache?

During autoregressive decoding, each new token attends to all previous tokens.
Without caching, every step recomputes K and V for the entire sequence — O(n²) cost.
KV cache stores the computed K and V tensors from previous steps and reuses them,
reducing each decode step to O(n).

```
step 1: token "Hello"
  compute K1, V1
  cache = [(K1, V1)]

step 2: token "world"
  compute K2, V2 only
  cache = [(K1,V1), (K2,V2)]   ← K1,V1 reused, not recomputed
  attention uses full cache

step N:
  compute Kn, Vn only
  cache grows by one entry per step
```

KV cache is a concept — any mechanism that stores K/V to avoid recomputation qualifies.
`DynamicCache` and `EncoderDecoderCache` are HuggingFace's concrete implementations of this concept.

---

### HuggingFace Cache Classes

| Class | Used for | seq dimension (CPU/GPU) | On NPU (QNN/HTP) |
|---|---|---|---|
| `tuple of tuples` | All models, transformers < 4.36 | grows each step | must use fixed shape + sliding window |
| `DynamicCache` | Decoder-only (LLaMA, GPT, Qwen) | grows each step | must use fixed shape + sliding window |
| `EncoderDecoderCache` | Encoder-decoder (Whisper, T5, BART) | sa grows, ca fixed | sa: fixed shape + sliding window; ca: fixed |
| `StaticCache` | Fixed-shape, torch.compile friendly | fixed | compatible — but HuggingFace may not support all models |
| `SlidingWindowCache` | Sliding-window models (Mistral) | fixed window | compatible concept, but export separately |

> ⚠️ **NPU requires fixed shapes.** `DynamicCache` grows dynamically on CPU/GPU, but QNN/HTP
> only accepts fixed-shape tensors. When exporting to ONNX for NPU, you must:
> 1. Choose a fixed `PAST_SEQ` value for the ONNX graph shape
> 2. Maintain fixed shape at runtime using a **sliding window** on the host side
>
> The `DynamicCache` / `EncoderDecoderCache` objects are only used **inside** the wrapper's
> `forward()` to satisfy HuggingFace's API — they are reconstructed from fixed-shape flat
> tensors each step and never actually grow.

**`DynamicCache`** — wraps a list of `DynamicLayer` objects, one per decoder layer:

```python
cache = DynamicCache()
# after one decode step (CPU/GPU):
cache.layers[0].keys    # [batch, heads, 1, head_dim]
# after N steps (CPU/GPU):
cache.layers[0].keys    # [batch, heads, N, head_dim]  ← grows dynamically on CPU/GPU

# On NPU: seq dimension is FIXED at PAST_SEQ
# The cache object is reconstructed from fixed-shape tensors each step:
cache = DynamicCache(ddp_cache_data=[(k, v) for k, v in past_kv_pairs])
cache.layers[0].keys    # [batch, heads, PAST_SEQ, head_dim]  ← always fixed
```

**`EncoderDecoderCache`** — holds two `DynamicCache` objects:

```python
cache = EncoderDecoderCache(
    self_attention_cache  = DynamicCache(),  # sa_kv: grows on CPU/GPU; FIXED on NPU
    cross_attention_cache = DynamicCache(),  # ca_kv: always fixed (from encoder, never changes)
)
cache.self_attention_cache.layers[0].keys   # sa_kv layer 0
cache.cross_attention_cache.layers[0].keys  # ca_kv layer 0
```

On NPU, both caches are reconstructed from fixed-shape tensors each step.
`sa_kv` uses sliding window to stay at `PAST_SEQ`; `ca_kv` is fed back unchanged.

**Version pitfalls** — internal attribute names changed across transformers versions:

| transformers version | cache format | layer key attribute |
|---|---|---|
| < 4.36 | `tuple of tuples` | `cache[layer][0]` (key), `cache[layer][1]` (value) |
| 4.36–4.45 | `DynamicCache` | `.key_cache[layer]`, `.value_cache[layer]` |
| 4.46–5.x | `DynamicCache` + `DynamicLayer` | `.layers[layer].keys`, `.layers[layer].values` |
| 4.46–5.x encoder-decoder | `EncoderDecoderCache` | `.self_attention_cache.layers[i].keys` |

Always inspect the actual cache object at runtime before writing export code:

```python
out = model.model.decoder(input_ids=..., encoder_hidden_states=..., use_cache=True)
pkv = out.past_key_values
print(type(pkv))                          # EncoderDecoderCache or DynamicCache
print(type(pkv.self_attention_cache))     # DynamicCache
print(dir(pkv.self_attention_cache.layers[0]))  # check attribute names
```

---

### Why KV Cache Must Be Exposed for NPU

Standard HuggingFace `model.generate()` manages KV cache internally as Python objects:

```python
# Standard flow — KV cache is hidden inside Python objects
output = model.generate(input_features)
# User never sees KV tensors — completely black-box
```

This works on CPU/GPU because Python objects can be passed between steps freely.
**NPU (QNN/HTP) cannot accept Python objects as inputs** — it only accepts fixed-shape tensors.

To run on NPU, KV cache must be **exposed** as explicit ONNX tensor inputs and outputs:

```
Standard:   model.forward(input_ids, past_kv_object) → (logits, present_kv_object)
NPU:        model.forward(input_ids, *past_kv_tensors) → (logits, *present_kv_tensors)
```

This requires three steps:

| Step | What | Why |
|---|---|---|
| **Expose** | Make KV tensors explicit ONNX I/O | NPU needs fixed tensor interface |
| **Flatten** | Cache object → flat tensor list | ONNX tracer cannot trace Python objects |
| **Reconstruct** | Flat tensors → cache object inside forward() | HuggingFace decoder expects cache object |

**Fixed shape + sliding window** — how NPU maintains fixed shape across steps:

```
ONNX graph (fixed):
  input:  sa_past_key  [1, heads, PAST_SEQ,   head_dim]
  output: sa_present_key [1, heads, PAST_SEQ+1, head_dim]

Host-side sliding window (after each step):
  sa_kv = present_kv[:, :, -PAST_SEQ:, :]   ← truncate back to PAST_SEQ

Result: ONNX graph always sees fixed [PAST_SEQ] input, never grows
```

The ONNX graph shape is always fixed. The sliding window runs on the **host CPU**
between decode steps — it is not part of the ONNX graph.

> ⚠️ **Always left-pad when building the initial window.** Place the prompt tokens at the **right end** of the fixed-length window and fill the left with pad tokens. Do not right-pad (prompt at the left).
> - **Right-pad** → prompt tokens are at positions `[0 … prompt_len-1]`; as the window slides right, they are the first to be dropped → model loses its prompt anchor → output degrades into repetition or incoherence within a few steps.
> - **Left-pad** → prompt tokens stay at the rightmost positions of the initial window; the window slides naturally without discarding the prompt until `SEQ_LEN` new tokens have been generated.
>
> ```python
> # ✅ Correct — left-pad
> if len(prompt_ids) < SEQ_LEN:
>     window = [PAD_ID] * (SEQ_LEN - len(prompt_ids)) + list(prompt_ids)
> else:
>     window = list(prompt_ids[-SEQ_LEN:])
>
> # ❌ Wrong — right-pad (prompt at left, pad at right)
> # window = list(prompt_ids) + [PAD_ID] * (SEQ_LEN - len(prompt_ids))
> ```

---

### KV Cache Exposure Pattern

The wrapper class handles all three steps:

```python
class DecoderWrapper(torch.nn.Module):
    def forward(self, decoder_input_ids, *past_kv_flat):
        # ── Step 1: Reconstruct cache object from flat tensors ──────────
        # past_kv_flat layout (decoder-only):
        #   [k0, v0, k1, v1, ..., k{N-1}, v{N-1}]
        n = NUM_DECODER_LAYERS
        past_key_values = tuple(
            (past_kv_flat[i*2], past_kv_flat[i*2+1])
            for i in range(n)
        )
        # For transformers >= 4.46, use DynamicCache:
        # cache = DynamicCache(ddp_cache_data=[(past_kv_flat[i*2], past_kv_flat[i*2+1]) for i in range(n)])

        # ── Step 2: Run decoder ─────────────────────────────────────────
        out = self.decoder(
            input_ids=decoder_input_ids,
            past_key_values=past_key_values,
            use_cache=True,
        )

        # ── Step 3: Flatten present KV back to tensors ──────────────────
        present_flat = []
        for layer_kv in out.past_key_values:
            present_flat.append(layer_kv[0])  # key
            present_flat.append(layer_kv[1])  # value
        # For DynamicCache:
        # for layer in out.past_key_values.layers:
        #     present_flat.extend([layer.keys, layer.values])

        logits = self.lm_head(out.last_hidden_state)[:, -1, :]
        return (logits, *present_flat)
```

**Encoder-decoder variant** (Whisper, T5) — two cache types, ca_kv fixed:

```python
class EncoderDecoderDecoderWrapper(torch.nn.Module):
    def forward(self, decoder_input_ids, *flat):
        # flat layout:
        #   [sa_k0, sa_v0, ..., sa_k{N-1}, sa_v{N-1},   ← self-attn, grows
        #    ca_k0, ca_v0, ..., ca_k{N-1}, ca_v{N-1}]   ← cross-attn, fixed
        n = NUM_DECODER_LAYERS
        sa_data = [(flat[i*2],     flat[i*2+1])     for i in range(n)]
        ca_data = [(flat[(n+i)*2], flat[(n+i)*2+1]) for i in range(n)]

        sa_cache = DynamicCache(ddp_cache_data=sa_data)
        ca_cache = DynamicCache(ddp_cache_data=ca_data)
        cache    = EncoderDecoderCache(sa_cache, ca_cache)

        out = self.decoder(
            input_ids=decoder_input_ids,
            encoder_hidden_states=encoder_hidden_states,
            past_key_values=cache,
            use_cache=True,
        )
        logits = self.proj_out(out.last_hidden_state)[:, -1, :]

        # Only expose sa_present — ca_kv is fixed, no need to output
        sa_present = []
        for layer in out.past_key_values.self_attention_cache.layers:
            sa_present.extend([layer.keys, layer.values])

        return (logits, *sa_present)
        # ca_kv is fed back unchanged from the input — caller reuses it as-is
```

**Key rule**: `ca_kv` (cross-attention) never changes during generation.
Feed the same `ca_kv` tensors into every decode step.
Only `sa_kv` (self-attention) needs to be updated via sliding window.

---

### ONNX Export with Exposed KV Cache

```python
# Decoder-only example
past_kv_dummy = [
    torch.zeros(1, NUM_HEADS, PAST_SEQ, HEAD_DIM)
    for _ in range(NUM_DECODER_LAYERS * 2)   # k and v for each layer
]

input_names = ["input_ids"] + [
    f"past_{'key' if i%2==0 else 'value'}.{i//2}"
    for i in range(NUM_DECODER_LAYERS * 2)
]
output_names = ["logits"] + [
    f"present_{'key' if i%2==0 else 'value'}.{i//2}"
    for i in range(NUM_DECODER_LAYERS * 2)
]

torch.onnx.export(
    decoder_wrapper,
    (input_ids_dummy, *past_kv_dummy),
    "decoder.onnx",
    opset_version=18,
    input_names=input_names,
    output_names=output_names,
    dynamic_axes=None,    # fixed shapes for HTP
    dynamo=False,         # required: forward uses *args
)
```

> ⚠️ Always use `dynamo=False` when `forward()` accepts `*args` for KV cache tensors.
> The new torch.export-based exporter fails with pytree mismatch errors on `*args` signatures.

---

## Objective

Export a transformer model into fixed-contract graph families for QNN/HTP deployment.

**Decoder-only models** (LLaMA, GPT, Qwen, etc.) — 2 graphs:
```text
1. Prefill graph: prompt -> initial KV cache
2. Decode graph: current token + past KV cache -> logits + present KV cache
```

**Encoder-decoder models** (Whisper, T5, BART, etc.) — 2 graphs (recommended):
```text
1. Encoder+CrossKV graph: audio/input -> cross-attention KV cache (run once)
2. Decode graph:          token + sa_kv + ca_kv -> logits + new sa_kv (run per token)
```

See [Encoder-Decoder Models](#encoder-decoder-models-whisper-t5-bart) for the full encoder-decoder contract.

For first HTP/NPU bring-up, a fixed-shape decode-only path is acceptable and often recommended:

```text
batch = 1
decode_seq = 1
past_seq = fixed small value, for example 32
```

Decode-only is not a complete serving pipeline, but it is a practical first milestone for validating model execution on QNN/HTP.

> 💡 **Hint — simplest first bring-up for decoder-only LLMs**: If the model supports it, export with `use_cache=False` and a single fixed input `input_ids [1, SEQ_LEN]`. This eliminates all KV-cache I/O complexity and is the fastest path to a working QNN artifact. Use a sliding window on the host side to maintain context across decode steps (see left-pad rule above). Upgrade to a full KV-cache export only after this baseline is validated on the target.

---

## ONNX Graph Families

### Prefill Graph

Prefill processes the prompt and creates the initial KV cache.

Expected inputs:

```text
input_ids: [batch, prefill_seq]
position_ids: [batch, prefill_seq]
attention_mask: optional, model/export dependent
```

Expected outputs:

```text
logits: [batch, prefill_seq, vocab_size] or [batch, vocab_size]
present_key_values.{layer}.key:   [batch, num_kv_heads, prefill_seq, head_dim]
present_key_values.{layer}.value: [batch, num_kv_heads, prefill_seq, head_dim]
```

Because QNN/HTP generally prefers fixed shapes, use one of these prefill strategies:

```text
fixed max prefill length + padding/truncation
multiple prefill buckets, for example 32/64/128/256
chunked prefill with a fixed chunk size
```

For bucketed prefill, create one ONNX/QNN graph per bucket:

```text
prefill_32.onnx
prefill_64.onnx
prefill_128.onnx
prefill_256.onnx
```

Runtime should select the smallest bucket that can hold the prompt.

---

### Decode Graph

Decode consumes the previous KV cache and produces next-token logits plus updated cache.

Expected inputs:

```text
input_ids: [batch, decode_seq]
position_ids: [batch, decode_seq]
past_key_values.{layer}.key:   [batch, num_kv_heads, past_seq, head_dim]
past_key_values.{layer}.value: [batch, num_kv_heads, past_seq, head_dim]
```

Expected outputs:

```text
logits: [batch, vocab_size] or [batch, decode_seq, vocab_size]
present_key_values.{layer}.key:   [batch, num_kv_heads, past_seq + decode_seq, head_dim]
present_key_values.{layer}.value: [batch, num_kv_heads, past_seq + decode_seq, head_dim]
```

Recommended first bring-up shape:

```text
batch = 1
decode_seq = 1
past_seq = fixed small value, for example 32
```

Do not treat this as the final decode graph:

```text
input_ids=[batch, prompt_len]
attention_mask=[batch, prompt_len]
use_cache=False
```

That full-forward path is useful as a baseline and resembles a prefill-style forward, but it is not a decode graph because it does not expose `past_key_values` and `present_key_values`.

---

## ONNX Export Requirements

### Prefill Export

The prefill export wrapper should:

1. Accept fixed or bucketed `input_ids`, `position_ids`, and optional `attention_mask`.
2. Call the model with `use_cache=True`.
3. Return logits and all `present_key_values` as explicit ONNX outputs.
4. Use fixed shapes, bucketed shapes, or chunked shapes suitable for the target NPU.

Conceptual HuggingFace wrapper:

```python
outputs = model(
    input_ids=input_ids,
    position_ids=position_ids,
    attention_mask=attention_mask,  # optional
    use_cache=True,
)
present = outputs.past_key_values
return logits_or_last_logits, *flatten(present)
```

Prefill output becomes the initial decode cache:

```text
prefill present_key_values -> decode past_key_values
```

---

### Decode Export

The decode export wrapper should:

1. Accept flattened `past_key_values` tensors as explicit forward inputs.
2. Reconstruct the model cache object or legacy tuple cache internally.
3. Call the model with `use_cache=True`.
4. Return last-token logits plus all `present_key_values` tensors as explicit ONNX outputs.

For HuggingFace models using `DynamicCache`:

```python
legacy_cache = tuple((past_key_i, past_value_i) for each layer)
cache = DynamicCache.from_legacy_cache(legacy_cache)
outputs = model(
    input_ids=input_ids,
    position_ids=position_ids,
    past_key_values=cache,
    use_cache=True,
)
present = outputs.past_key_values.to_legacy_cache()
return outputs.logits[:, -1, :], *flatten(present)
```

For models that still use legacy tuple cache directly, pass the tuple cache directly.

---

## KV-Cache I/O Contract

Here `contract` means an engineering interface agreement between the exported ONNX graph and the runtime script.

Prefill contract:

```text
feed prompt tensors
read present_key_values
convert present_key_values to initial decode past_key_values
```

Decode contract:

```text
feed current token input_ids
feed current token position_ids
feed fixed-shape past_key_values
read logits
read present_key_values
```

For fixed-cache decode, update cache with sliding window/truncation:

```python
next_past = present[:, :, -past_seq:, :]
```

This is needed when `present_seq = past_seq + decode_seq` but the next decode graph still expects `past_seq`.

> 💡 **Hint — remote target inference scripts**: Remote target devices often have only `numpy` and `onnx` installed — `transformers` (and `AutoTokenizer`) is typically absent. Write inference scripts that do not import `transformers` at runtime on the target. Recommended approach: tokenize the prompt on the host, hardcode the resulting token ID list in the inference script, and decode output token IDs back to text on the host after collecting results.

### Choosing `past_seq`

`past_seq` is the single most impactful fixed-shape parameter for decode quality.
Too small → the sliding window discards early context → decoder loses grounding → repetition loops or incoherent output.
Too large → higher per-step memory and latency on HTP.

**Sizing rule of thumb:**

```
past_seq ≥ max_expected_output_tokens × 1.5
minimum recommended: 64
```

Examples:

| Use case | Typical output length | Recommended past_seq |
|---|---|---|
| Short classification / tagging | < 10 tokens | 64 (minimum) |
| Sentence-level transcription (e.g. Whisper 5s) | ~20–50 tokens | 128 |
| Paragraph summarisation | ~100–200 tokens | 256–512 |
| Long-form generation / chat | 256–1024 tokens | 512–2048 |

**Bring-up vs production:**

- Use a small value (e.g. `past_seq=32`) only for initial HTP bring-up to keep binary size small and iteration fast.
- Before declaring acceptance, increase `past_seq` to match the real workload and re-validate end-to-end.
- A model that passes bring-up at `past_seq=32` but degenerates at `past_seq=128` has a real quality issue — do not ship the small value as a workaround.

**Bucketed approach (optional):**

If the target workload has variable output length, export multiple decode graphs with different `past_seq` values and select at runtime:

```python
# Example buckets
BUCKETS = [64, 128, 256, 512]
past_seq = min(b for b in BUCKETS if b >= current_generated_length)
```

Each bucket requires a separate context binary. The tradeoff is binary count vs. quality coverage.

---

## ONNX Patch Requirements

After export, inspect and patch ONNX for QAIRT/QNN compatibility.

Common patches:

---

### HuggingFace Decoder Export Blockers

Several issues arise specifically when exporting HuggingFace decoder models with the legacy
TorchScript exporter (`dynamo=False`). These are not visible in dry-run and must be resolved
at export time.

#### `aten::diff` — not exportable to ONNX

**Source:** `masking_utils.find_packed_sequence_indices()` — called when `position_ids` is
passed and the model checks for packed sequences.

**Symptom:**
```
torch.onnx.errors.UnsupportedOperatorError: Exporting the operator aten::diff
to ONNX opset version 18 is not supported
```

**Fix — patch the function before export:**
```python
import transformers.masking_utils as _mu

def _patched_find_packed_sequence_indices(position_ids, is_tracing=None):
    return None   # single sequence, no packing needed

_mu.find_packed_sequence_indices = _patched_find_packed_sequence_indices
```
Apply this patch at the top of the export script, before any model import.

#### `Cast(BOOL_8 → FLOAT_16)` — HTP hard rejection

**Source:** `attention_mask` flowing through `masking_utils.py` produces a
`Cast(int64→BOOL) → And → Cast(BOOL→float)` subgraph — one instance per decoder layer.
The QNN converter accepts it silently; `qnn-context-binary-generator` rejects it at compose time.

**Symptom (context binary generation):**
```
QnnBackend_validateOpConfig failed 3110
in[0]:QNN_DATATYPE_BOOL_8  out[0]:QNN_DATATYPE_FLOAT_16
MODEL_GRAPH_OP_VALIDATION_ERROR
```

**Fix — pass `attention_mask=None` in the export wrapper:**
```python
outputs = model(
    input_ids=input_ids,
    attention_mask=None,   # model builds pure float causal mask; no BOOL cast
    position_ids=position_ids,
    use_cache=False,
)
```
Do not attempt ONNX surgery — the subgraph repeats per layer and is impractical to patch post-export.

#### Dynamic `Gather` input leaking as graph input

**Source:** `logits[:, -1, :]` with a dynamic index causes the index to leak as an unbound
graph input (e.g. `onnx::Gather_N`), which breaks conversion.

**Fix — use a slice + squeeze instead of direct index:**
```python
logits = outputs.logits[:, -1:, :].squeeze(1)   # fixed slice, no dynamic Gather input
```

#### Recommended export wrapper skeleton (decoder-only, `use_cache=False`)

Combining all three fixes:
```python
import transformers.masking_utils as _mu

_mu.find_packed_sequence_indices = lambda position_ids, is_tracing=None: None

class DecoderWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, position_ids):
        out = self.model(
            input_ids=input_ids,
            attention_mask=None,
            position_ids=position_ids,
            use_cache=False,
        )
        return out.logits[:, -1:, :].squeeze(1)   # [batch, vocab_size]

torch.onnx.export(
    DecoderWrapper(model),
    (dummy_input_ids, dummy_position_ids),
    "model.onnx",
    input_names=["input_ids", "position_ids"],
    output_names=["logits"],
    opset_version=18,
    dynamo=False,
)
```

> ⚠️ Use `attn_implementation="eager"` when loading the model to avoid SDPA ops that may not
> export cleanly with the legacy exporter.

---

#### Qwen3 — known export blockers (verified on Qwen3-0.6B / 1.7B)

Qwen3 hits all three blockers above. Confirmed fix sequence:

| # | Blocker | Fix |
|---|---------|-----|
| 1 | `aten::diff` via `find_packed_sequence_indices` | Patch function to return `None` before model load |
| 2 | `Cast(BOOL→FP16)` in causal mask | `attention_mask=None` in wrapper |
| 3 | Dynamic `Gather` from `logits[:, -1, :]` | Use `logits[:, -1:, :].squeeze(1)` |

Additional Qwen3 notes:
- Load with `attn_implementation="eager"` and `torch_dtype=torch.float32` for export
- Set `model.config.use_cache = False` before tracing
- `LessOrEqual: unsupported version` appears in dry-run — this is a benign warning, conversion succeeds
- Context binary for QCS9075 / SA8775P: `soc_id=77`, `dsp_arch=v73`

```text
Reciprocal(x) -> Div(1.0, x)
ReduceMean axes tensor input -> axes attribute
Reshape: remove allowzero attribute
```

QAIRT 2.42 documentation says ONNX conversion supports up to ONNX Opset 22, but some newer op schema forms may still be risky in the QNN/HTP path. Compatibility patches may still be required.

---

### ReduceMean Patch

Example:

```text
Before:
ReduceMean(hidden_states, reduce_axes_const)
attributes: keepdims=1, noop_with_empty_axes=0

After:
ReduceMean(hidden_states)
attributes: axes=[-1], keepdims=1
```

Also remove `noop_with_empty_axes` when lowering to older schema compatibility.

---

### Reshape Patch

Example:

```text
Before:
Reshape(input_tensor, target_shape), allowzero=0

After:
Reshape(input_tensor, target_shape)
```

---

### Causal Mask Patch Guidance

Check whether the exported graph contains this subgraph:

```text
Cast -> GatherND -> And -> Where
```

If that subgraph exists and includes `Cast(INT64 -> BOOL_8)`, HTP may reject it.

Under a fixed decode condition such as:

```text
decode_seq = 1
past_seq = 32
present attention length = 33
attention mask = all ones
```

the additive attention mask can sometimes be replaced with:

```text
zeros([1, 1, 1, 33], float32)
```

Conceptually:

```text
Before:
attention_scores + Where(mask_condition, 0, -inf)

After:
attention_scores + zeros([1, 1, 1, 33])
```

Only apply this if the subgraph exists and the all-ones fixed-decode assumption is valid. Do not apply it blindly to dynamic prompt, padding, or arbitrary attention-mask cases.

---

## Inference Script Requirements

The inference/generation script must support both prefill and decode.

### Prefill Runtime Requirements

1. Select the correct fixed prefill graph or bucket.
2. Feed prompt tensors with correct padding/truncation or chunking.
3. Read `present_key_values`.
4. Convert present cache layout/names into decode `past_key_values`.

### Decode Runtime Requirements

1. Feed `input_ids`, `position_ids`, and all `past_key_values` raw/tensor inputs.
2. Handle QNN name mutation, for example:

```text
past_key_values.0.key -> past_key_values_0_key
present_key_values.0.key -> present_key_values_0_key
```

3. Use runtime-reported shape and dtype.
4. Handle layout mutation for cache tensors if QNN reports a different shape.
5. Select the output named `logits`, not blindly `outputs[0]`.
6. Save or reuse all `present_key_values` outputs.
7. Update cache by sliding window/truncation for the next decode step.

QNN may mutate output order. `outputs[0]` may be a present KV tensor rather than logits.

---

## Validation Requirements

Validate prefill and decode separately before validating generation.

### Prefill Validation

```text
Compare ONNX Runtime prefill logits and present KV outputs against QNN output.
Check output shape, nonzero count, cosine similarity, MAE, and max_abs_diff.
```

### Decode Validation

```text
Compare ONNX Runtime decode logits and present KV outputs against QNN output.
Check output shape, nonzero count, cosine similarity, MAE, and max_abs_diff.
```

### End-to-End Validation

```text
Run prefill once.
Use prefill present KV as decode past KV.
Run one or more decode steps.
Verify logits remain close to ONNX Runtime or PyTorch reference.
```

At minimum report:

```text
shape
sum
norm
max
nonzero count
cosine similarity
MAE
max_abs_diff
```

Acceptance guidance:

```text
Wrapper selects intended artifact.
QNN logits are nonzero.
Cosine similarity should be high enough for FP16, commonly >= 0.99 and preferably >= 0.999 for simple bring-up.
```

---

## Failed Paths to Avoid

Do not use full-forward export as final decode path:

```text
input_ids=[batch,prompt_len]
use_cache=False
```

If a host-generated context binary returns all-zero outputs for a model, do not use it as the acceptance path without further debugging:

```text
<model_patched>.onnx.so.bin
```

Do not assume `outputs[0]` is logits.

Do not leave stale `.onnx.so.bin` next to ONNX if you want wrapper to select `.onnx.so`.


---

## Encoder-Decoder Models (Whisper, T5, BART)

Encoder-decoder models have two distinct transformer stacks:

```
Encoder: processes the full input (audio mel, source text) once
Decoder: generates output tokens one at a time, attending to encoder output via cross-attention
```

Each decoder layer has **two** attention mechanisms:

| Attention type | Attends to | KV cache behaviour |
|---|---|---|
| Self-attention | Previously generated tokens | Grows each step → needs sliding window |
| Cross-attention | Encoder hidden states | Fixed for entire generation → compute once |

---

### Recommended: 2-Graph Pipeline

Merge the encoder and cross-KV projection into a single graph.
This is the recommended deployment pattern — fewer artifacts, fewer context binaries, no intermediate tensor transfer between graphs.

```
Graph 1: Encoder + Cross-KV projector  (run once per input)
  input : source input (e.g. mel [1, 80, 3000])
  output: ca_key.{0..N-1}, ca_value.{0..N-1}   each [1, heads, src_seq, head_dim]

Graph 2: Decoder  (run once per output token)
  input : decoder_input_ids [1, 1]
          sa_past_key.{0..N-1}, sa_past_value.{0..N-1}   each [1, heads, past_seq, head_dim]
          ca_key.{0..N-1},      ca_value.{0..N-1}         each [1, heads, src_seq, head_dim]
  output: logits [1, vocab_size]
          sa_present_key.{0..N-1}, sa_present_value.{0..N-1}   each [1, heads, past_seq+1, head_dim]
```

`ca_kv` is computed once from Graph 1 and fed unchanged into every Graph 2 call.
Only `sa_kv` needs the sliding window update between steps.

**Why not output `encoder_hidden_states` from Graph 1?**
The decoder only needs the projected cross-attention KV tensors, not the raw encoder hidden states.
Projecting inside Graph 1 avoids re-running `k_proj` / `v_proj` on every decode step.
If you need `encoder_hidden_states` for another purpose (e.g. embedding extraction), add it as an extra output — it does not affect the decoder pipeline.

---

### Export Pattern

```python
class EncoderCrossKVWrapper(torch.nn.Module):
    """Graph 1: encoder + cross-attention KV projection."""
    def __init__(self, model):
        super().__init__()
        self.encoder       = model.model.encoder
        self.decoder_layers = model.model.decoder.layers

    def forward(self, input_features):
        enc_hidden = self.encoder(input_features).last_hidden_state
        ca_kvs = []
        for layer in self.decoder_layers:
            bsz, src_len, _ = enc_hidden.shape
            k = layer.encoder_attn.k_proj(enc_hidden)
            v = layer.encoder_attn.v_proj(enc_hidden)
            k = k.view(bsz, src_len, NUM_HEADS, HEAD_DIM).transpose(1, 2)
            v = v.view(bsz, src_len, NUM_HEADS, HEAD_DIM).transpose(1, 2)
            ca_kvs.extend([k, v])
        return tuple(ca_kvs)   # 2 × N tensors, each [1, heads, src_seq, head_dim]


class DecoderWrapper(torch.nn.Module):
    """Graph 2: one decode step with explicit sa_kv + ca_kv inputs."""
    def forward(self, decoder_input_ids, *flat):
        # flat layout: [sa_k0, sa_v0, ..., sa_kN, sa_vN, ca_k0, ca_v0, ..., ca_kN, ca_vN]
        n = NUM_DECODER_LAYERS
        sa_data = [(flat[i*2], flat[i*2+1]) for i in range(n)]
        ca_data = [(flat[(n+i)*2], flat[(n+i)*2+1]) for i in range(n)]
        # reconstruct cache object and run decoder ...
        # return (logits, *sa_present_flat)
```

Export both with `dynamo=False` (required when `forward` uses `*args`):

```python
torch.onnx.export(enc_cross_kv_wrapper, (mel_dummy,),
                  "model-encoder-cross-kv.onnx", opset_version=18,
                  input_names=["input_features"],
                  output_names=[f"ca_{'key' if i%2==0 else 'value'}.{i//2}"
                                for i in range(N_LAYERS * 2)])

torch.onnx.export(decoder_wrapper,
                  (dec_input_ids, *sa_dummy, *ca_dummy),
                  "model-decoder.onnx", opset_version=18,
                  input_names=["decoder_input_ids"] + sa_names + ca_names,
                  output_names=["logits"] + sa_present_names,
                  dynamo=False)
```

---

### KV-Cache Management

```python
# Initialise before generation
sa_kv = [np.zeros((1, NUM_HEADS, PAST_SEQ, HEAD_DIM), dtype=np.float32)
         for _ in range(NUM_DECODER_LAYERS * 2)]

# Run Graph 1 once
ca_kv = encoder_cross_kv_session.run(None, {"input_features": mel})

# Decode loop
for step in range(MAX_NEW_TOKENS):
    outputs = decoder_session.run(None,
        {"decoder_input_ids": token} | sa_feed | ca_feed)
    logits      = outputs[0]                        # or use output name
    sa_present  = outputs[1 : 1 + NUM_DECODER_LAYERS * 2]

    # Sliding window — only sa_kv, never ca_kv
    sa_kv = [t[:, :, -PAST_SEQ:, :] for t in sa_present]

    next_token = np.argmax(logits[0])
    if next_token == EOT_TOKEN:
        break
```

Key rules:
- `ca_kv` is **never updated** — feed the same tensors every step
- `sa_kv` uses sliding window — only self-attention history grows
- Always select logits **by output name**, not by index — QAIRT may reorder outputs (see `inference.md`)

---

### Alternative: 3-Graph Pipeline

Split encoder and cross-KV projection into separate graphs.
Only use this if you need `encoder_hidden_states` as a standalone output for another downstream task.

```
Graph 1: Encoder only          input → encoder_hidden_states
Graph 2: Cross-KV projector    encoder_hidden_states → ca_key/value × N
Graph 3: Decoder               (token, sa_kv, ca_kv) → (logits, new_sa_kv)
```

This adds one extra context binary and one extra graph execution per input.
For pure transcription / translation use cases, the 2-graph pipeline is preferred.

---

### Deployment Checklist (Encoder-Decoder)

- [ ] `model-encoder-cross-kv.onnx` + `.onnx.data` (if external data) + `.yaml`
- [ ] `model-decoder.onnx` + `.onnx.data` (if external data) + `.yaml`
- [ ] Context binaries: `model-encoder-cross-kv.onnx.so.bin`, `model-decoder.onnx.so.bin`
- [ ] Both `.yaml` files deployed alongside `.onnx` on target (wrapper output reorder depends on them)
- [ ] `ca_kv` initialised from Graph 1 before decode loop starts
- [ ] Sliding window applied to `sa_kv` only — never to `ca_kv`

---

## Final Instruction

Build the formal solution as prefill + decode. For initial HTP/NPU bring-up or fallback, implement the fixed decode KV-cache path first and validate it end-to-end. Then add prefill buckets/chunks and validate the prefill-to-decode cache handoff.
