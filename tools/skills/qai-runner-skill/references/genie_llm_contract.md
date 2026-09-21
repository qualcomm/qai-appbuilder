# Genie LLM Graph Contract (making a self-converted decoder loadable by `genie-t2t-run`)

> **Knowledge scope**: what a self-converted decoder `.bin` must satisfy for the SDK's own
> Genie runtime (`Genie.dll` / `genie-t2t-run.exe`) to load and drive it. Use this when the
> goal is "hand the AR loop to Genie" instead of writing a host-side Python AR loop.
> **Source**: QAIRT SDK Genie source at `<qairt_sdk_root>/examples/Genie/Genie/src`
> (SDK 2.48.40.260702). Every rule below carries a `file:line`. Paths are relative to
> `.../Genie/src/qualla/engines/` unless stated otherwise.
> **Evidence tier**: SDK source = binding contract (the shipped `Genie.dll` is what we load).
> Nothing here has been validated on device yet — see § Not yet measured.

> ⚠️ The Genie README states the library internals may be refactored and only the **Genie C
> API** is a stable interface. Re-verify these line numbers against your own SDK version
> before relying on them.

---

## 1. Why this document exists

A decoder graph can be numerically perfect and still be rejected — or worse, **silently
produce garbage** — because Genie infers structure from tensor names, shapes, and
quantization encodings. Two of the rules below (mask encoding, RoPE pairing) fail with **no
error message at all**.

---

## 2. Tensor naming

Genie does **not** hardcode a tensor list for KV cache; it pattern-matches.

```cpp
// qnn-api/qnn-utils.hpp:234-237  — isKVTensor()
return (s.ends_with("_in") || s.ends_with("_out")) &&
       (s.find("key") != npos || s.find("value") != npos);
```

- Prefix default is `past_` (`qnn-htp/nsp-model.hpp:174`), overridable via
  `cache-groups.prefix` (`qnn-htp/nsp-utils/nsp-params.cpp:349`, mandatory when that block
  is present).
- **Suffix `_in` / `_out` is required.** A common export names outputs `present_key_0`;
  rename to `past_key_0_out` (input stays `past_key_0_in`).
- **`_key` → `_value` is a literal substring replacement** (`nsp-model.cpp:500-502`), so the
  words must be underscore-delimited tokens. Mismatch → error
  `"Found Key {} but no Value {}"` (`nsp-model.cpp:1063-1067`).
- **Layer index = the first two integers in the name** (`qnn-utils.hpp:217-220`, packed
  `layer<<16|head`) ⇒ put exactly one number in a KV tensor name. `past_key_0_in` is fine;
  `layer3_past_key_0_in` is not.

Recognised non-KV names (`nsp-model.hpp:46-68`, 21 entries) include `input_ids` (:47),
`logits` (:48), `attention_mask` (:52), `position_ids_sin` (:53), `position_ids_cos` (:54),
`position_ids` (:55), `inputs_embeds` (:58).

**Presence of `inputs_embeds` auto-selects embedding-input mode** — no config switch
(`nsp-model.cpp:667-669`). Convenient: the same graph shape used for a host-side AR loop is
already the shape Genie expects.

---

## 3. Shapes (`checkShape`, `nsp-model.cpp:470-497`)

`Dims` order is batch, height, width, channel, bitwidth (`qnn-utils.hpp:58-63`).
`AR` = tokens per step, `CL` = context length.

**⚠️ How a 4D tensor maps onto `Dims` — get this wrong and every KV shape looks incomprehensible.**
`Dims(const std::vector<uint32_t>&)` reads **`dims.at(1), at(2), at(3)`**
(`qnn-utils.cpp:66-67`); `at(0)` is folded into `height` by a `// Hack to mix batch dimension`
(`:70-71`). So for a 4D tensor, `checkShape`'s `(height, width, channel)` are the **last three
dims**, and **dim 0 is never validated**. A 4D KV tensor `[n_kv, 1, A, B]` is therefore checked
as `width=A, channel=B` with `n_kv` ignored.

| Tensor | Required shape | Line |
|---|---|---|
| `attention_mask` | `[1, AR, CL]` | `:858` |
| `position_ids_sin` / `_cos` | `[1, AR, rope_dims]` | `:870-873` |
| `position_ids` (ABSOLUTE) | `[1, 1, AR]` | `:877` |
| `position_ids` (ALIBI) | `[1, AR, CL]` | `:881` |
| `logits` | numel = `vocab` or `vocab*AR` | `:793-798` |
| KV in — key | `[*, kv_dim, past_dim]` | `:903` |
| KV in — value | `[*, past_dim, kv_dim]` | `:905` |
| KV out — key | `[*, kv_dim, AR]` | `:911` |
| KV out — value | `[*, AR, kv_dim]` | `:913` |

**`past_dim = use_scatter ? CL : CL - AR`** (`nsp-model.cpp:894-895`). Note keys are stored
**transposed** relative to values.

**Concrete, verified layout** for `n_kv` KV heads and `kv_dim = head_dim`:

```
past_key_<L>_in    [n_kv, 1, kv_dim,   past_dim]      past_dim = CL - AR
past_value_<L>_in  [n_kv, 1, past_dim, kv_dim  ]
past_key_<L>_out   [n_kv, 1, kv_dim,   AR      ]      <- delta only, TRANSPOSED
past_value_<L>_out [n_kv, 1, AR,       kv_dim  ]      <- delta only
```

⚠️ **KV outputs carry only the NEW columns, not the accumulated window.** Genie owns the ring
buffer host-side (§ 8). A graph that maintains its own cache in-graph and returns the full
window is incompatible and must be re-exported.

> Verified on device: a graph exported to exactly these shapes converts, passes all
> `checkShape` rules read back from the compiled `.bin`, and matches an HF fp32 reference
> (`past_key_0_out` cosine 1.0000000, `logits` 0.9999924). See § Measured status.

---

## 4. 🛑 Attention mask encoding — silent-failure rule #1

**Genie does not quantize the mask.** For a quantized mask tensor it writes fixed integer
codes and **ignores the tensor's scale/offset entirely**:

```cpp
// qnn-htp/nsp-model.cpp:1372-1379
if (attn_quantized) {
  if (m_modelArchitectureType == ModelArchitectureType::ENCODER) {
    m_attention_positive_value.u32 = 1;
  } else {
    m_attention_positive_value.u32 = 0xffffffff;  // u8=0xff  u16=0xffff
  }
  m_attention_negative_value.u32 = 0;
}
```

(`grep t_attn_mask->quantParam nsp-model.cpp` → zero matches.)

⇒ **The encoding's maximum representable value must be exactly `0.0`**, because `0xFFFF`
means "attend". For `UFIXED_POINT_16` with dequant `real = scale * (code + offset)`:

```
offset = -65535
scale  = |floor| / 65535
```

With `floor = -100`: `scale = 100/65535 = 0.0015259021893143654`, giving
`code 0xFFFF → 0.0` (attend) and `code 0x0000 → -100.0` (masked).

> ❌ **A symmetric encoding such as `[-100, +100]` makes "attend" dequantize to `+100`** —
> attention inverts, output is fluent garbage, and **nothing errors**.

The `-100` floor itself is **our choice, not a Genie constant**. Genie's own non-quantized
paths use different values: HTP float/fp16 mask uses `-1000.0f`
(`nsp-model.cpp:1386, 1388`), the GPU engine uses `-10000` (`qnn-gpu/gpu-model.hpp:34`).

**Mask geometry** — the CL axis is split in two, not simply left/right aligned:

```cpp
// qnn-htp/KVCache/smart-mask.cpp:36-41
if (m_useScatter) return step.n_valid_kv;
else              return step.ctx_size - step.variant;   // new_idx = CL - AR
```

| Range | Contents |
|---|---|
| `[0, CL-AR)` | history KV region, left-aligned from 0 (`KVCache/kvmanager.cpp:448-450`) |
| `[CL-AR, CL)` | K/V slots for this step's AR tokens |

CAUSAL mode therefore writes **two spans** per row (`qnn-htp/attention-mask.cpp:72-79`),
collapsing to one only when the cache is exactly full. Buffer is pre-filled with the
negative value, then attend spans are stamped in (`nsp-model.cpp:1524-1536`);
`fillAttentionRow` only takes the positive value (`attention-mask.hpp:63-70`). Explicit
instantiations exist for `uint8/16/32` only (`attention-mask.cpp:315-360`) — **no float
version**, confirming the mask is treated as opaque codes.

Rows `>= n_process` are left fully masked ⇒ a short prompt is handled by masking, **not** by
changing shape.

---

## 5. 🛑 RoPE — silent-failure rule #2

Genie builds a `[CL, rope_dim]` table and memcpys it in. **Nothing in Genie describes how
the graph consumes it**, so the pairing convention is fixed by **your weights**, not by Genie.

```cpp
// qnn-htp/nsp-model.cpp:3621-3627
const double exponent = 1.0 / static_cast<double>(pos_dim);
if (posEncoding.rope_params.freqs_type == PositionalEncoding::DEFAULT) {
  for (uint32_t j = 0; j < pos_dim; j++) { inv_freq[j] = 1.0 / pow(theta, j * exponent); }
```

- **`inv_freq[j] = theta^(-j/rope_dim)`, j = 0..rope_dim-1, NOT duplicated to head_dim.**
  Numerically identical to HuggingFace's `theta^(-2i/head_dim)` because
  `head_dim = 2 * rope_dim`.
- Angles: `freqs[i][j] = i * inv_freq[j]` (`nsp-model.cpp:3900-3905`).
- **`rope-dim` = number of frequency pairs = head_dim / 2** (for head_dim=64 → 32). It is
  *not* a partial-rotary count — that is `partial-rotary-factor` (`nsp-model.cpp:3724`).
- **`head_dim` is never read in the RoPE path** (`pos_dim = rope-dim` only,
  `nsp-model.cpp:79-82`, `:3584`) ⇒ Genie **cannot detect** a `rope-dim` / `head_dim`
  mismatch. `kv-dim` is not consulted here.
- ⇒ For a HF-Llama-lineage model (unpermuted Q/K weights) use **`rotate_half` / split-half**:
  lane `j` pairs with `j+32`; the graph must broadcast the 32-wide table to 64 via
  `concat([t, t], -1)`. Interleaved (`rotate_every_two`) would need
  `repeat_interleave(t, 2, -1)`. **Genie cannot tell which you built.**
- Corroboration that `DEFAULT` is the non-pre-duplicated form: the `CONCAT` freqs-type
  halves the index to pre-duplicate a 64-wide table (`nsp-model.cpp:3628-3632`); default is
  `DEFAULT` (`nsp-params.cpp:272`).

**Unlike the mask, cos/sin DO honour the tensor's encoding:**

```cpp
// qnn-htp/nsp-model.cpp:3614-3619
auto [q_scale, q_offset] = posData.t_position_ids_cos->quantParam[0];
if (posData.d_pos == QNN_DATATYPE_FLOAT_16 || posData.d_pos == QNN_DATATYPE_FLOAT_32) {
  q_scale = 1.0; q_offset = 0;   // If floating point, don't quantize!
}
```

- ⚠️ **Only the `cos` tensor's params are read, then reused for `sin`** ⇒ cos and sin **must
  share one encoding**.
- ⚠️ **Truncation, not rounding** (`static_cast`, `nsp-model.cpp:3913`), with the SDK's own
  comment: `// round() instead of floor() seems to produce an acuracy drop. To debug later`.
  Expect ±1 LSB drift against a rounding reference.
- ⚠️ **Boundary overflow**: with `scale=1/32768, offset=-32768`, `cos(0)=1` computes to
  65536 → `static_cast<uint16_t>` wraps to **0** at `pos=0, j=0` (`:3924`). An encoding
  covering exactly `[-1, +1]` sits on the boundary.
- `rope-theta` is parsed as `int32_t` (`nsp-params.cpp:271`) then widened to double
  (`:3588`) ⇒ non-integer or `>2^31` theta silently truncates. `100000` is safe.
- `{"type":"rope","rope-dim":32,"rope-theta":100000}` with no `rope-scaling` selects
  `rope_type = DEFAULT` (`nsp-params.cpp:46`, `:273`; enum name `"default"` at `:17`;
  validated list `Dialog.cpp:874-878`) and applies **no scaling** — llama3 (`:3635`),
  longrope (`:3659`), yarn (`:3683`), linear (`:3714`), proportional (`:3719`) all skipped;
  `attention_factor` stays `1.0` (`:3634`).
- The **vision** tower uses a different convention (`inv_freq(freq_dim / 2)`,
  `nsp-image-model.cpp:757-758`) — do not copy it into the LM tower.
- Debug aid: `_debug_tensors` dumps `position_ids_{sin,cos}.u16.dat`
  (`nsp-model.cpp:3946-3954`) for diffing against a reference.

---

## 6. AR / CL inference — shapes first, name only as fallback

`ar<N>_cl<M>_<i>_of_<K>` is **not mandatory**. AR is derived from shapes, with the graph name
as a last resort.

**AR cascade** (`qnn-htp/nsp-graph.cpp:72-142`, first hit wins): `anchor`→`new_keys` (:78) →
`input_ids` (:83) → `inputs_embeds` (:90) → two `embed_tokens_Gather` output names (:97, :104)
→ `attention_mask` (:111) → any output `past_*key*`.channel (:119) → `logits` (:125) →
**regex `(ar|AR)_?(\d+)`** on the graph name (:135-138) → **throw** (:140-141)
`"Unexpected model. Couldn't determine required input tokens "`.

**CL cascade** (`nsp-graph.cpp:146-187`): output `score`.channel (:151) →
`attention_mask`.channel (:154) → `key_out.channel + key_in.channel` (:158) →
**regex `(cl|CL)_?(\d+)`** (:170-173) → **largest integer in the name if `> 501`** (:175-185)
→ **return `-1`** (:186). `#define CONTEXT_SAFE_LIMIT 501` at `:30`; the comment at `:28-29`
explains it exists so the `N` in `AR-N` is not mistaken for CL.

**Failure behaviour is asymmetric:**

| Failure | Result |
|---|---|
| AR undeterminable | **hard throw** (`nsp-graph.cpp:140-141`) |
| CL undeterminable | soft — `ctx_size = -1` (`:186`), variant broadcast to every detected CL (`nsp-model.cpp:334-348`) |
| no CL anywhere | `"Genie is not able to determine the context length for some of the graphs. Please name the graph properly."` (`KVCache/kvmanager.cpp:283-288`) |
| duplicate `{AR, CL}` | throw `"qnn-htp: duplicate graph found, likely overflow occured"` (`nsp-graph.cpp:297-302`) |

> **Recommendation**: still name graphs `ar128_cl4096_1_of_1` — it satisfies every fallback
> *and* the shape path, so no single mistake is fatal.

---

## 7. AR variant selection — there is no prefill/decode flag

Selection happens in `KVManager::prepareInferenceStrategy`
(`qnn-htp/KVCache/kvmanager.cpp:365-518`), which builds a full `InferenceStrategy` up front.

```cpp
// KVCache/kvmanager.cpp:386-406  (comment verbatim)
// This is a simple lambda function that returns the smallest choice larger or equal to n
// If no such choice exists, the largest choice is returned
```

- CL is chosen first (`lower_bound(n_valid_kv)`), then AR within it (`:408-409`).
- **`pick` is called ONCE before the chunking loop** (`:409`); `variant` stays fixed for
  every iteration of `while (n_remain > 0)` (`:430-449`). It is only re-picked on a **CL
  switch** (`:439-445`).
- Decode is the degenerate case: `n_inputs == 1` → `pick(1, {1,128})` → `ar1`.

⭐ **A non-multiple remainder is PADDED on the same graph — it never falls back to a smaller
one.** With `{1, 32, 128}` and a 300-token prompt: `pick(300)` finds no `arN >= 300`, so it
returns the largest (128); chunks are **128, 128, 44**, and the 44 runs on **ar128 padded**.

> **Design consequence**: adding an intermediate AR (e.g. `ar32`) does **not** speed up the
> tail of a long prompt. It only helps when the *whole* prompt is short (a 20-token prompt
> picks `ar32`). For prefill+decode, **`{ar128, ar1}` is the right pair** — a third variant
> buys nothing for long prompts.

Multiple variants do coexist without limit: `std::map<CL, std::set<AR>>`
(`kvmanager.hpp:402`, sorted `kvmanager.hpp:385-396`).

---

## 8. KV cache is aliased, not copied

Allocation is one block laid out `[in_region | out_region]` (`qnn-api/QnnApi.cpp:940-958`):
`_in` at `offset`, `_out` at `offset + sizeof(_in)`; pairing loop at `nsp-model.cpp:1008-1016`.

⇒ **KV `_in` and `_out` encodings must match per layer** (same scale/offset), since they are
two windows on one buffer. **NOT FOUND in source: any validation that enforces this** — a
mismatch is therefore another silent-corruption path.

Graph classification (`nsp-graph.cpp:245-261`): `inputs_embeds` + KV out + `logits` →
`GraphType::DEFAULT`, the ordinary path. `DECODER` requires *no* `input_ids`/`inputs_embeds`
and *no* `logits` (`:247-251`).

---

## 9. Backend config keys that matter

| Key | Default | Reality |
|---|---|---|
| `enable-graph-switching` | `false` (`qnn-htp.cpp:76`) | Adds exactly two `QnnContext_Config_t` entries — `MEMORY_LIMIT_HINT`=1024 MB (hardcoded) and `PERSISTENT_BINARY`=true (`QnnApi.cpp:124-127`) — and pins the context buffer for process life (`:877-880`) instead of freeing it (`:1275-1276`). **No graph-level config is touched.** A 2-graph weight-shared `.bin` still loads and runs with it `false`; only AR-switch cost rises. |
| `use-mmap` | `true` (`qnn-htp.cpp:62`) | With switching on, the pinned buffer's *kind* decides RSS: mmap = file-backed and evictable (`QnnApi.cpp:820-857`); `false` = `new uint8_t[...]` (`:865`), whole `.bin` in anonymous RAM forever. Warning at `nsp-image-model.cpp:70-71`. **Keep `true`.** |
| `mmap-budget` | `0` (`qnn-htp.cpp:58`) | Only applied when `> 0` → `QNN_HTP_CONTEXT_CONFIG_OPTION_FILE_READ_MEMORY_BUDGET` (`QnnApi.cpp:1200-1205`). Independent of switching. |
| `spill-fill-bufsize` | `0` (`qnn-htp.cpp:65`) | **Bytes.** Groups **multiple contexts (multiple `.bin` files)** onto one spill-fill buffer via `REGISTER_MULTI_CONTEXTS` (`QnnApi.cpp:1225-1236`). ⚠️ **Not** related to multi-graph weight sharing inside one context — with a single `.bin` only `contextIdx == 0` exists, so it has **no effect**. Also ignored on the async path (`QnnApi.cpp:1380`, parameter commented out), and async is the default (`qnn-htp.cpp:64`). |
| `kv-dim` | ⚠️ **inconsistent**: `128` at `qnn-htp.cpp:66`, `-1` at `nsp-utils/nsp-params.cpp:352` | Two code paths disagree, so **omitting this key gives a path-dependent default**. Set it explicitly (= head_dim). |

> **Lesson**: do not copy a backend config block from another project wholesale. At least one
> commonly-copied key (`spill-fill-bufsize` on a single-`.bin` async setup) is **inert**, and
> `kv-dim`'s default is ambiguous. Verify each key against the source.

---

## 10. Sharding (`ctx-bins`, `i_of_K`)

**`i_of_K` is never parsed.** The only occurrence of `_of_` in Genie source is a *comment*
(`nsp-graph.cpp:176`). Shards are **counted**, then ordered by **lexicographic sort** of
graph names: graphs are bucketed per `{AR,CL}` into a `std::set<std::string>`
(`nsp-model.cpp:323-325`), `n_splits = max(count)` (`:354-358`), and split indices assigned
by iterating that set (`:366-372`, comment: *"Graph names are sorted by default
(std::set<>), so iterate by split"*).

> ⚠️ **With ≥10 shards, `_10_of_16` sorts before `_2_of_16`** → layer order scrambles.
> Zero-pad shard indices.

Each shard holds a **disjoint subset of layers**, executed in ascending index order
(`nsp-model.cpp:2470-2505`, `:2660-2694`). Hidden state flows between shards with **no
explicit copy** — shard *n*'s output and shard *n+1*'s input share one name-keyed buffer
(`QnnApi.cpp:1015-1029`; `nsp-model.cpp:991` *"all buffers link to the same address
anyway"*) ⇒ **inter-shard tensor names must match exactly.** Files and shards are
independent dimensions: `ctx-bins` is mandatory (`Dialog.cpp:758`), validated (`:775-781`),
one context per file (`QnnApi.cpp:1117`, `:1215`, `:1456`).

---

## 10b. Multimodal: Genie runs the vision tower natively

A VLM does **not** need a host-side AR loop or host-side embedding fusion. Genie ships a
node graph that does both. This is reachable from **`genie-app`**, not `genie-t2t-run`
(grep for `Pipeline|ImageEncoder` in `genie-t2t-run/main.cpp` → **no matches**; the pipeline
API lives in `genie-app/include/GeniePipeline.hpp`).

### Node types (`pipeline/Node.cpp:192-211`)

`lut-encoder` / `text-encoder` / `text-generator` / `image-encoder` / `diffuser` /
`lm-executor`. Any other key throws `"Unknown config key: "` (`:210`).

### The reference topology is architecturally identical to SmolVLM

`genie-app/scripts/glm-4v` — a SigLIP vision tower + LUT text embedder + LLM, i.e. our exact
shape:

```
node config create imageEncoderConfig siglip-htp.json
node config create lutEncoderConfig   text-encoder.json
node config create textGeneratorConfig glm-4v-htp.json
pipeline connect GeniePipeline imageEncoder GENIE_NODE_IMAGE_ENCODER_EMBEDDING_OUTPUT \
                              textGenerator GENIE_NODE_TEXT_GENERATOR_EMBEDDING_INPUT
pipeline connect GeniePipeline lutEncoder  GENIE_NODE_TEXT_ENCODER_EMBEDDING_OUTPUT \
                              textGenerator GENIE_NODE_TEXT_GENERATOR_EMBEDDING_INPUT
node set text  lutEncoder   GENIE_NODE_TEXT_ENCODER_TEXT_INPUT "...<|user|>"
node set image imageEncoder GENIE_NODE_IMAGE_ENCODER_IMAGE_INPUT preprocessed_image_dog.raw
node set text  lutEncoder   GENIE_NODE_TEXT_ENCODER_TEXT_INPUT "Describe the image.\n<|assistant|>\n"
pipeline execute GeniePipeline
```

Note the LLM graph names in its LoRA file: `ar1_cl4096_1_of_2-ar128_cl4096_1_of_2` — the
same `{ar128, ar1}` two-variant layout § 7 recommends.

### ⭐ Fusion is by APPEND ORDER, not by scanning for an image-token id

`Accumulator` is one flat embedding buffer. Each `append` concatenates and advances a token
counter (`pipeline/Accumulator.cpp:28-53`). There is **no `image_token_id`**, no
`masked_scatter`, no placeholder search anywhere in this path.

⇒ **Interleaving is expressed by the ORDER of the `node set text` / `node set image`
commands.** In the script above, text-before-image-before-text is what puts the 64 image
embeddings in the middle of the prompt. The template's `<image>` placeholder token is
simply *not emitted* by the LUT encoder; position is positional.

### Encoding is renegotiated automatically

The accumulator's destination encoding is taken from the **LLM's** input quant param
(`pipeline/TextGenerator.cpp:112-119` → `getInputQuantParam`), and every `append` requantizes
from the source encoding to it (`Accumulator.cpp:45-48`, `quantization::requantize`). The
vision tower's output scale/offset comes from `getOutputQuantParam`
(`pipeline/ImageEncoder.cpp:134`).

⇒ **The vision tower and the LLM do NOT need a shared encoding.** Genie bridges them. This
removes a whole class of feared work (co-calibrating both towers to one scale).

### Vision-tower input names are a CLOSED set (`pipeline/ImageEncoder.cpp:51-71`)

Only these are accepted: `pixel_values`, `position_ids_sin`, `position_ids_cos`,
`full_attention_mask`, `window_attention_mask`, `pretile_embedding`, `posttile_embedding`,
`gated_pos_embedding`. **Any other input name throws** `"ImageEncoder meet unsupported input
layer of model"` (`:66-67`).

✅ Our vision graph's single input is `pixel_values` → compliant as exported.

Also auto-forced for an image-encoder node: `pooled-output=false`, `disable-kv-cache=true`
(`ImageEncoder.cpp:32-33`). `vision-param.{height,width}` is read at `:46-47` and only feeds
mRoPE (`TextGenerator.cpp:301-305`, gated on `m_usingMRope`) — **SmolVLM does not use mRoPE,
so `vision-param` is unnecessary for us.** Output embeddings are named `image_embeddings` in
the DataLoader path (`ImageEncoder.cpp:122`).

### Consequence for this project

Our hand-written host-side AR loop + numpy `embed_tokens` lookup + `masked_scatter` fusion
is **replaceable end-to-end by stock Genie**, using three JSON configs and a `genie-app`
script. The `-e` / `-t` route via `genie-t2t-run` (§ how a colleague's artifact was run) is
the *lower*-level path that requires host-side fusion; the pipeline path does not.

> **Evidence tier**: SDK source, read directly. **Not yet run.** Unverified: whether
> `siglip-htp.json` / `text-encoder.json` schemas accept our tensor layout, and whether the
> LUT encoder can be pointed at our `embed_tokens_weight.npy` converted to its binary format.

---

## 10c. Embedding-input path (`-e` / `-t`) and the LUT binary format

This is the *lower*-level route (host does the fusion), used by `genie-t2t-run`. § 10b is the
higher-level alternative.

### LUT file layout: headerless row-major, no metadata whatsoever

`-t PATH[,TYPE,SCALE,OFFSET]` (`genie-t2t-run/main.cpp:145-156`) is bulk-read into a heap
buffer — **no magic bytes, no dims, no dtype tag, no row count**; the only check is
`size <= 0` (`main.cpp:453-469`, `:454-457`). Indexing:

```cpp
// main.cpp:625-629
size_t lutIndex = static_cast<size_t>(token) * embeddingSize;  // embeddingSize is BYTES
std::copy(embeddingSrc, embeddingSrc + embeddingSize, embeddingDst);
```

⇒ **`byte_offset = token_id * row_bytes`, row-major `[n_vocab][embed_dim]`, no padding.**
Verified arithmetically against the third-party artifact: `49280 * 960 * 4 = 189,235,200` B =
exactly its `embedding_fp32_lut.bin`.

**Two independent LUT implementations exist and they differ:**

| | `genie-t2t-run` CLI | library-internal `qualla::LUT` |
|---|---|---|
| Config | `-t` arg only; JSON `embedding.datatype` **never consulted** (`main.cpp:449`, `:63`) | JSON key `lut-path` (`encoders/text-encoders/LUT.cpp:34`) |
| Load | bulk `read` into `new int8_t[]` (`main.cpp:460`) | **mmap** (`LUT.cpp:69-75`) |
| Row bytes | engine-computed `getEmbeddingBufferSize()` | `n_embd * bitWidth/8` from config datatype (`LUT.cpp:47-58`, `:93`) |
| Index type | `size_t` (`main.cpp:625`) | ⚠️ `uint32_t` (`LUT.cpp:96`, `:149`) — **overflows above 4 GiB** |

⚠️ **Silent-corruption path**: the LUT's row stride must equal what the engine computes. The
only guard is an end-of-buffer overflow test (`main.cpp:626`, `:654`), which **cannot detect a
wrong-but-smaller stride** — a dtype mismatch just reads misaligned rows and generates
plausible garbage.

### Requantization on the way in

`-t` accepts `TYPE` from `{int8,uint8,int16,uint16}` only (`main.cpp:248-249`) — **`float32`
is rejected in the 4-field form** even though it is the 1-field default (`main.cpp:63`).
Affine requant is precomputed once:

```cpp
// main.cpp:636-643
g_requantScale  = g_lutScale / g_inputScale;
g_requantOffset = g_requantScale * g_lutOffset - g_inputOffset;
to[i] = static_cast<T>(g_requantScale * from[i] + g_requantOffset);
```

⚠️ `static_cast` again — **truncation toward zero, no rounding, no clamping**; out-of-range
values wrap. Same class of hazard as the RoPE table (§ 5). An unmapped dtype pair throws
`"Unsupported LUT requantization: "` (`main.cpp:1088-1089`).

`-t` requires `-e` (`main.cpp:572-574`). Both are mutually exclusive with `--prompt` /
`--prompt_file` / `--tokens_file`.

---

## 10d. Config schema: unknown keys throw (mostly)

**Good news for debugging: a config that parses has no misspelled keys.** Every validator is
an `if/else-if` chain over `item.key()` ending in `throw GENIE_STATUS_ERROR_JSON_SCHEMA`.
Confirmed terminal-else in: `dialog` (`Dialog.cpp:2526-2528`), `context`
(`Context.cpp:106-108`), `sampler` (`Sampler.cpp:379-381`), `engine` (`Dialog.cpp:1426-1428`),
`backend` (`:526-528`), **`QnnHtp` (`:359-361`)**, `model` (`:1027-1029`), `binary`
(`:785-787`), `positional-encoding` (`:921-924`), `cache-groups` (`:1347-1350`), `tokenizer`
(`:110-113`), `embedding` (`:176-179`).

**The exceptions — keys that are silently ignored:**

| Path | Behaviour | Citation |
|---|---|---|
| `engine.longcontext.keydiff` | chain ends at `anchor-alpha` with **no terminal else** | `Dialog.cpp:1109-1118` |
| `dialog.nested-generator`, `dialog.nested-policy` | contents **not validated**, only type-checked | `Dialog.cpp:2522-2525` |
| `dialog.accumulator-size` | accepted with **no type check at all** | `Dialog.cpp:2457-2458` |
| `dialog.callback-type` | deferred to pipeline TextGenerator | `Dialog.cpp:2459-2460` |

### Mandatory fields (missing → hard throw)

- `dialog`: `{version, type, context, tokenizer, engine}` (`Dialog.cpp:2406`). **`sampler` and
  `embedding` are NOT mandatory.** `version` must equal `1` (`:2431-2436`).
- `dialog.context`: `{version, bos-token, eos-token, size, n-vocab}` (`Context.cpp:34`).
  **`pad-token` is optional** (`:65-66`).
- `dialog.sampler`: `{version}` (per Sampler validator).
- **Every block carries its own `"version": 1`.**

### Exact key spellings for `dialog.context` (`Context.cpp:44-70`)

`version`, `bos-token`, `eos-token`, `eot-token`, **`img-token`**, `size`, `n-vocab`,
`draft-n-vocab`, `pad-token`, `n-embd`, `grammar`.

⚠️ They are `bos-token` / `eos-token` / `pad-token` — **not** `bos` / `eos` / `pad`.
`eos-token` accepts **numeric OR array** (`:53-54`) → multiple EOS ids are supported.
**`img-token` exists** (`:57-58`) but is absent from the third-party VLM config, consistent
with § 10b's finding that fusion is positional rather than placeholder-scanned.

`dialog.type` ∈ `{basic, ssd-q1, lade, spd, multistream, eaglet, kv-share}`
(`Dialog.cpp:2440-2456`). `spd` and `kv-share` force `dialog.engine` to be a **2-element array**
with `role` on each (`:1360-1364`, `:1440-1483`).

> **Evidence tier**: SDK source. Two sub-agent reports were truncated before their
> "NOT FOUND" sections; anything not listed above should be treated as unconfirmed rather than
> absent. Defaults for `max-num-tokens` and `pad-token` were not located.

---

## 11. Export checklist

1. Tensor names: `inputs_embeds`, `attention_mask`, `position_ids_cos`, `position_ids_sin`,
   `past_key_<L>_in` / `past_value_<L>_in` / `past_key_<L>_out` / `past_value_<L>_out`,
   `logits`. One integer per KV name.
2. Shapes per § 3, including the **transposed key** layout and `past_dim = CL - AR`.
3. RoPE: table is `rope_dim = head_dim/2` wide, `theta^(-j/rope_dim)`, **split-half**
   pairing in-graph via `concat([t,t],-1)` for HF-lineage weights.
4. Quantization overrides: mask `offset=-65535, scale=|floor|/65535`; cos/sin sharing one
   encoding; KV `_in`/`_out` identical per layer.
5. Graph name `ar<N>_cl<M>_1_of_1`.
6. Two graphs `{ar128, ar1}` with weight sharing for prefill+decode; no third variant.
7. Config: explicit `kv-dim`, `use-mmap: true`, omit `spill-fill-bufsize` for a single `.bin`.
8. For a VLM, prefer the `genie-app` pipeline (§ 10b) over a host-side AR loop: vision input
   must be named `pixel_values`, and prompt interleaving is expressed by command order.

---

## Measured status

**Verified on device (Snapdragon X Elite, HTP v73, QAIRT 2.48.40.260702)** with a 2-layer
AR=1/CL=128 probe graph exported to the rules in § 2, § 3 and § 5:

| Claim | Evidence |
|---|---|
| The naming + shape rules are satisfiable by `qairt-converter` + `qnn-context-binary-generator` | `.bin` built, 134,334,728 bytes |
| The compiled `.bin` really carries the contract shapes | I/O read back with `qnn-context-binary-utility`, then run through a re-implementation of Genie's own validators — **22/22 rules pass** (`:858`, `:871-873`, `:903/:905`, `:911/:913`, `:793-798`, `isKVTensor`, one-integer-per-name, `_key`->`_value` swap) |
| The rewrite (external RoPE, 3D mask, delta-only transposed KV) preserves the math | vs HF fp32 eager: `logits` cosine **0.9999924**, `past_key_0_out` **1.0000000**, `past_value_0_out` **0.9999999**, argmax match, top-5 5/5 |
| **RoPE split-half is correct for HF-lineage weights** | host-built cos/sin tables using Genie's `theta^(-j/rope_dim)` vs HF `apply_rotary_pos_emb`: **max abs diff = 0.000e+00** (exact) |
| A contract-compliant graph is still drivable by a **custom host loop** | the numerical run above was driven by Python + `qai_appbuilder`, not by Genie |

**Make the RoPE check an assertion in the export script.** It costs one forward pass, needs no
conversion, and it is the only way to catch the one mistake Genie can never report.

### Still not measured

- **Whether `Genie.dll` / `genie-t2t-run.exe` actually loads such a `.bin`.** Passing a
  re-implementation of `checkShape` is not the same as passing the real loader; static rule
  checking is not execution.
- Whether `qairt-quantizer` can emit the exact mask encoding (`offset=-65535`,
  `scale=|floor|/65535`) via `--quant_overrides`. **All measurements above are fp16**, where
  the encoding constraints of § 4 do not yet bite.
- Anything in § 10b (multimodal pipeline), § 10c (LUT format) or § 10d (config schema) —
  source-read only.
- Multi-AR (`ar128`) and weight sharing across two graphs.
