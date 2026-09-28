# Transformer Decoder PyTorch Modification for HTP/NPU Prefill + Decode

Use this reference when asking an AI coding agent to modify a PyTorch/HuggingFace transformer decoder project so it can be exported into HTP/NPU-friendly prefill and decode graphs.

This file focuses on PyTorch/HuggingFace model-side changes: model configuration discovery, wrapper structure, cache handling, and fixed-shape bring-up assumptions.

For ONNX export contracts, ONNX graph patching, QNN/QAIRT runtime behavior, inference scripts, and validation details, see:

```text
transformer_models_qairt.md
```

---

## Workflow

Use this workflow when adapting a PyTorch/HuggingFace transformer decoder for HTP/NPU-friendly prefill and decode execution.

### 1. Discover model configuration

Read model dimensions and attention settings from the HuggingFace config:

```text
num_hidden_layers
num_attention_heads
num_key_value_heads
hidden_size
head_dim
vocab_size
max_position_embeddings
rope settings if applicable
```

Use these values to derive the KV-cache tensor shapes and wrapper signatures. Do not hardcode model-specific values unless the document is explicitly describing a model-specific example.

### 2. Choose the first bring-up target

Start with the smallest useful decode path:

```text
batch = 1
decode_seq = 1
past_seq = fixed small value, for example 32
```

This fixed-shape decode path is usually the fastest way to validate that the model can run with explicit KV-cache inputs and outputs. It is not the final serving solution, but it is the recommended first milestone.

### 3. Implement the decode wrapper

Create a PyTorch wrapper that:

```text
current token + position_ids + flat past KV cache
    -> logits + flat present KV cache
```

The wrapper must expose KV tensors explicitly at the forward boundary instead of hiding them inside HuggingFace cache objects.

### 4. Validate decode in PyTorch

Before export, run the decode wrapper directly in PyTorch and verify:

```text
logits shape
present KV count
present KV shapes
dtype
finite values
nonzero count
fixed-cache truncation behavior
```

### 5. Add prefill wrapper for the complete pipeline

After fixed decode works, add a prefill wrapper:

```text
prompt tokens -> logits + initial present KV cache
```

The prefill present cache becomes the initial decode past cache.

### 6. Validate prefill-to-decode handoff

Run the PyTorch prefill wrapper once, feed its present KV cache into the decode wrapper, and verify that decode logits and cache shapes remain valid.

### 7. Continue with ONNX/QNN work

After PyTorch wrappers are validated, continue with ONNX export, ONNX patching, QNN/QAIRT conversion, runtime script integration, and QNN validation using:

```text
transformer_models_qairt.md
```

---

## Architecture Overview

### Prefill

Prefill processes prompt tokens and builds the initial KV cache.

Conceptual input/output shape contract:

```text
input_ids: [batch, prefill_seq]
position_ids: [batch, prefill_seq]
attention_mask: optional, model/export dependent
outputs:
  logits: [batch, prefill_seq, vocab_size] or [batch, vocab_size]
  present_key_values.{layer}.key/value: [batch, num_kv_heads, prefill_seq, head_dim]
```

Because HTP/NPU generally prefers fixed shape, prefill usually needs one of these strategies:

```text
fixed max prefill length + padding/truncation
multiple prefill buckets, for example 32/64/128/256
chunked prefill with a fixed chunk size
```

---

### Decode

Decode consumes the previous cache and produces next-token logits plus updated cache.

Conceptual input/output shape contract:

```text
input_ids: [batch, decode_seq]
position_ids: [batch, decode_seq]
past_key_values.{layer}.key:   [batch, num_kv_heads, past_seq, head_dim]
past_key_values.{layer}.value: [batch, num_kv_heads, past_seq, head_dim]
outputs:
  logits: [batch, vocab_size] or [batch, decode_seq, vocab_size]
  present_key_values.{layer}.key:   [batch, num_kv_heads, past_seq + decode_seq, head_dim]
  present_key_values.{layer}.value: [batch, num_kv_heads, past_seq + decode_seq, head_dim]
```

Recommended initial bring-up shape:

```text
batch = 1
decode_seq = 1
past_seq = fixed small value, for example 32
```

---

## Model Configuration Discovery

Read these values from the model config; do not hardcode unless documenting a model-specific example:

```text
num_hidden_layers
num_attention_heads
num_key_value_heads
hidden_size
head_dim
vocab_size
max_position_embeddings
rope settings if applicable
```

If `head_dim` is missing:

```text
head_dim = hidden_size / num_attention_heads
```

If `num_key_value_heads` is missing, check whether the model uses standard MHA, MQA, or GQA. For standard MHA:

```text
num_key_value_heads = num_attention_heads
```

Use these values to build dummy tensors, cache tensors, wrapper signatures, and output names consistently.

---

## PyTorch Wrapper Requirements

Create explicit PyTorch wrapper modules for the prefill and decode paths.

The wrappers should:

1. Put the model in `eval()` mode.
2. Disable training-only behavior.
3. Use `torch.no_grad()` during validation and sample execution.
4. Use `use_cache=True`.
5. Expose KV cache tensors as explicit inputs/outputs instead of hidden Python objects.
6. Keep shape assumptions visible and documented in the wrapper constructor or export script.
7. Avoid relying on dynamic Python-side control flow in the exported forward path.

---

## Prefill Wrapper Requirements

Implement prefill wrapping when the goal is a complete prompt-to-generation pipeline.

The prefill wrapper should:

1. Accept fixed or bucketed `input_ids`, `position_ids`, and optional `attention_mask`.
2. Call the model with `use_cache=True`.
3. Return logits and all `present_key_values` as explicit outputs.
4. Support fixed shapes, bucketed shapes, or chunked shapes suitable for the target NPU.

Conceptual HuggingFace wrapper:

```python
class PrefillWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model.eval()

    def forward(self, input_ids, position_ids, attention_mask=None):
        outputs = self.model(
            input_ids=input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            use_cache=True,
        )
        present = outputs.past_key_values
        logits = outputs.logits
        return logits, *flatten_cache(present)
```

If only last-token logits are needed by the serving path, return:

```python
outputs.logits[:, -1, :]
```

instead of the full sequence logits.

Prefill output becomes the initial decode cache:

```text
prefill present_key_values -> decode past_key_values
```

---

## Decode Wrapper Requirements

Implement decode wrapping for both formal serving and fallback bring-up.

The decode wrapper must:

1. Accept flattened `past_key_values` tensors as explicit forward inputs.
2. Reconstruct the model cache object or legacy tuple cache internally.
3. Call the model with `use_cache=True`.
4. Return last-token logits plus all `present_key_values` tensors as explicit outputs.

For HuggingFace models using `DynamicCache`:

```python
from transformers.cache_utils import DynamicCache

class DecodeWrapper(torch.nn.Module):
    def __init__(self, model, num_layers):
        super().__init__()
        self.model = model.eval()
        self.num_layers = num_layers

    def forward(self, input_ids, position_ids, *flat_past):
        legacy_cache = tuple(
            (flat_past[2 * i], flat_past[2 * i + 1])
            for i in range(self.num_layers)
        )
        cache = DynamicCache.from_legacy_cache(legacy_cache)

        outputs = self.model(
            input_ids=input_ids,
            position_ids=position_ids,
            past_key_values=cache,
            use_cache=True,
        )

        present = outputs.past_key_values.to_legacy_cache()
        return outputs.logits[:, -1, :], *flatten_cache(present)
```

For models that still use legacy tuple cache directly, pass the tuple cache directly:

```python
outputs = self.model(
    input_ids=input_ids,
    position_ids=position_ids,
    past_key_values=legacy_cache,
    use_cache=True,
)
present = outputs.past_key_values
return outputs.logits[:, -1, :], *flatten_cache(present)
```

Do not treat this as the final decode path:

```text
input_ids=[batch, prompt_len]
attention_mask=[batch, prompt_len]
use_cache=False
```

That full-forward path is useful as a PyTorch baseline and resembles a prefill-style forward, but it is not a decode path because it does not expose `past_key_values` and `present_key_values`.

---

## Cache Flattening Helpers

Use stable helper functions to flatten and reconstruct cache tensors.

Example:

```python
def flatten_cache(cache):
    flat = []
    for key, value in cache:
        flat.extend([key, value])
    return tuple(flat)
```

For reconstruction:

```python
def unflatten_cache(flat_cache, num_layers):
    return tuple(
        (flat_cache[2 * i], flat_cache[2 * i + 1])
        for i in range(num_layers)
    )
```

Keep the ordering stable:

```text
layer0.key
layer0.value
layer1.key
layer1.value
...
```

The same order must be used by export, runtime input feeding, runtime output reading, and validation.

---

## DynamicCache API by transformers Version

The HuggingFace \DynamicCache\ API has changed across \	ransformers\ versions.
When extracting KV tensors from \outputs.past_key_values\ after a forward pass,
use the pattern that matches your installed version:

| transformers version | KV access pattern | Example |
|---------------------|-------------------|---------|
| < 4.40 | Subscriptable: \past_kv[layer_idx]\ returns \(key, value)\ tuple | \k, v = past_kv[layer_idx]\ |
| 4.40 - 4.46 | \.key_cache\ / \.value_cache\ lists | \k = past_kv.key_cache[i]; v = past_kv.value_cache[i]\ |
| >= 4.47 | \.layers[i].keys\ / \.layers[i].values\ (iterable, not subscriptable) | \k = past_kv.layers[i].keys; v = past_kv.layers[i].values\ |

To detect the available API at runtime:

\\python
if hasattr(past_kv, 'layers'):
    # transformers >= 4.47
    k, v = past_kv.layers[i].keys, past_kv.layers[i].values
elif hasattr(past_kv, 'key_cache'):
    # transformers 4.40 - 4.46
    k, v = past_kv.key_cache[i], past_kv.value_cache[i]
else:
    # transformers < 4.40 (or legacy tuple cache)
    k, v = past_kv[i]
\
**Common errors and their causes:**

| Error | Likely transformers version | Fix |
|-------|---------------------------|-----|
| \TypeError: 'DynamicCache' object is not subscriptable\ | >= 4.47 | Use \.layers[i].keys\ / \.values\ |
| \AttributeError: 'DynamicCache' object has no attribute 'key_cache'\ | >= 4.47 | Use \.layers[i].keys\ / \.values\ |
| \AttributeError: 'DynamicCache' object has no attribute 'layers'\ | < 4.47 | Use \.key_cache[i]\ or subscript |

---

## Fixed-Cache Decode Behavior

For fixed-cache decode, the model may produce:

```text
present_seq = past_seq + decode_seq
```

while the next decode invocation still expects:

```text
past_seq = fixed value
```

The runtime or validation harness must therefore use sliding-window truncation:

```python
next_past = present[:, :, -past_seq:, :]
```

The PyTorch reference path should implement the same truncation when comparing against ONNX/QNN outputs.

---

## Attention Mask and Causal Mask Notes

Decoder models use causal masking so the current token cannot attend to future tokens.

For prefill, causal masking is usually required because multiple prompt tokens are processed together.

For fixed single-token decode:

```text
decode_seq = 1
past_seq = fixed
attention mask = all ones
```

the current token can attend to all past tokens plus itself. In that specific case, the additive attention mask may be equivalent to all zeros.

Do not remove or bypass model-side masking assumptions unless the decode shape and all-ones attention assumption are valid.

Detailed ONNX-level causal-mask patching guidance is maintained in:

```text
transformer_models_qairt.md
```

---

## PyTorch Validation Before Export

Before ONNX/QNN work, validate the PyTorch wrappers directly.

Check prefill:

```text
prefill wrapper runs with fixed or bucketed input shape
prefill logits shape is expected
prefill present KV shapes are expected
```

Check decode:

```text
decode wrapper accepts explicit flat past KV tensors
decode logits shape is expected
decode present KV shapes are expected
fixed-cache truncation produces the next valid past cache
```

At minimum print or assert:

```text
logits shape
number of cache tensors
cache tensor shapes
dtype
nonzero count
finite values
```

The ONNX/QNN validation flow is described in:

```text
transformer_models_qairt.md
```

---

## Failed Paths to Avoid

Do not use full-forward export as final decode path:

```text
input_ids=[batch,prompt_len]
use_cache=False
```

Do not hide KV cache inside Python objects at the wrapper boundary.

Do not assume every HuggingFace model uses the same cache class. Check whether the model expects:

```text
DynamicCache
legacy tuple cache
model-specific cache class
```

Do not hardcode model dimensions unless they are explicitly documented as a model-specific example.

Do not change mask behavior for dynamic prompt, padding, or arbitrary attention-mask cases unless the model semantics are preserved.

---

## Final Instruction

Build the PyTorch-side solution as explicit prefill and decode wrappers. For initial HTP/NPU bring-up or fallback, implement and validate the fixed-shape decode KV-cache path first. Then add prefill buckets or chunks and validate the prefill-to-decode cache handoff before proceeding to ONNX/QNN conversion guidance in `transformer_models_qairt.md`.