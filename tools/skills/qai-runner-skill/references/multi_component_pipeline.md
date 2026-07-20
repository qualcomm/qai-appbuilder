# Multi-Component Pipeline Export & Conversion

Use this reference when the source model is a **pipeline** composed of multiple independent
sub-models (e.g. Stable Diffusion, Whisper encoder+decoder, CLIP image+text towers, BLIP,
LLaVA vision+language, etc.) that must each be exported, converted, and deployed separately.

> **When to use this reference**:
> - `MODEL_NAME` maps to a pipeline class (e.g. `StableDiffusionPipeline`, `WhisperModel`)
>   rather than a single `nn.Module`
> - The model has distinct components with different I/O shapes or data types
> - You need separate context binaries per component
> - `ONNX_FILE = {MODEL_NAME}.onnx` does not make sense (there is no single ONNX file)

---

## 1. Config Variables for Multi-Component Projects

Replace the single `MODEL_NAME` / `ONNX_FILE` pair in `aipc_plan.md` with a component list:

```
MODEL_NAME    = <pipeline name, e.g. stable-diffusion-v1-5>
COMPONENTS    = <comma-separated component names, e.g. text_encoder, unet, vae_decoder>
ONNX_DIR      = <directory holding all component ONNX files, e.g. onnx_models>

# Derived per-component (resolve {COMP} for each entry in COMPONENTS):
ONNX_FILE     = {ONNX_DIR}/{COMP}.onnx          # e.g. onnx_models/text_encoder.onnx
LIB_FILE      = lib{COMP}.so                     # Linux model library
CTX_BIN       = {COMP}.onnx.so.bin               # context binary (ONNX-match name)
```

All downstream phases (inspection, conversion, context binary, inference) iterate over
`COMPONENTS` and apply the same steps to each entry.

---

## 2. Identifying Components

### `diffusers` pipelines (Stable Diffusion, SDXL, etc.)

| Component | Class | Typical I/O |
|---|---|---|
| `text_encoder` | `CLIPTextModel` | `input_ids [B,77] int32` → `last_hidden_state [B,77,768]` |
| `unet` | `UNet2DConditionModel` | `sample [B,4,H/8,W/8]`, `timestep [B]`, `encoder_hidden_states [B,77,768]` → `out_sample [B,4,H/8,W/8]` |
| `vae_decoder` | `AutoencoderKL` (decode only) | `latent_sample [B,4,H/8,W/8]` → `sample [B,3,H,W]` |
| `vae_encoder` | `AutoencoderKL` (encode only) | `sample [B,3,H,W]` → `latent [B,4,H/8,W/8]` *(img2img only)* |

### Whisper

| Component | Typical I/O |
|---|---|
| `encoder` | `input_features [B,80,3000]` → `last_hidden_state [B,1500,512]` |
| `decoder` | `input_ids [B,T]`, `encoder_hidden_states [B,1500,512]` → `logits [B,T,vocab]` |

### CLIP

| Component | Typical I/O |
|---|---|
| `vision_model` | `pixel_values [B,3,224,224]` → `image_embeds [B,512]` |
| `text_model` | `input_ids [B,77]` → `text_embeds [B,512]` |

---

## 3. Extracting Sub-Models from a Pipeline

### `diffusers` — key rules

```python
from diffusers import StableDiffusionPipeline
import torch

pipe = StableDiffusionPipeline.from_pretrained(
    "path/to/model",
    torch_dtype=torch.float32,
    safety_checker=None,
)
# ⚠️ Pipeline objects are NOT nn.Module — they have no .eval()
# Call .eval() on each sub-model individually:
text_encoder = pipe.text_encoder.eval()
unet         = pipe.unet.eval()
vae          = pipe.vae.eval()
```

### VAE decoder wrapper (decode-only export)

Export only the decoder half of the VAE to avoid exporting the encoder when it is not needed:

```python
class VaeDecoderWrapper(torch.nn.Module):
    def __init__(self, vae):
        super().__init__()
        self.vae = vae

    def forward(self, latent_sample):
        # Apply scaling factor before decode
        latent_sample = latent_sample / 0.18215
        return self.vae.decode(latent_sample).sample

vae_decoder = VaeDecoderWrapper(pipe.vae).eval()
```

---

## 4. Export Pattern

Use `dynamo=False` for all `diffusers` components (see `model_export_validation.md` §Legacy exporter).
Export each component to its own file inside `ONNX_DIR`:

```python
import os, torch, onnx, onnxsim
os.makedirs(ONNX_DIR, exist_ok=True)

COMPONENTS = {
    "text_encoder": {
        "model":   text_encoder,
        "args":    (torch.zeros(1, 77, dtype=torch.int32),),
        "inputs":  ["input_ids"],
        "outputs": ["last_hidden_state", "pooler_output"],
    },
    "unet": {
        "model":   unet,
        "args":    (
            torch.zeros(1, 4, 64, 64),   # sample
            torch.tensor([1.0]),          # timestep
            torch.zeros(1, 77, 768),      # encoder_hidden_states
        ),
        "inputs":  ["sample", "timestep", "encoder_hidden_states"],
        "outputs": ["out_sample"],
    },
    "vae_decoder": {
        "model":   vae_decoder,
        "args":    (torch.zeros(1, 4, 64, 64),),
        "inputs":  ["latent_sample"],
        "outputs": ["sample"],
    },
}

for name, cfg in COMPONENTS.items():
    onnx_path = f"{ONNX_DIR}/{name}.onnx"
    with torch.no_grad():
        torch.onnx.export(
            cfg["model"], cfg["args"], onnx_path,
            opset_version=18,
            input_names=cfg["inputs"],
            output_names=cfg["outputs"],
            dynamic_axes=None,   # fixed shapes for QNN
            dynamo=False,        # required for diffusers components
        )
    # Use path-based check (safe for >2 GB models)
    onnx.checker.check_model(onnx_path)
    print(f"  {name}: OK")

    # onnxsim — skip gracefully on external-data models
    try:
        m = onnx.load(onnx_path)
        m_sim, ok = onnxsim.simplify(m)
        if ok:
            onnx.save(m_sim, onnx_path)
    except Exception as e:
        print(f"  {name}: onnxsim skipped ({e})")
```

---

## 5. Conversion — iterate over components

Run `aipc_convert_fp.py` (or `aipc_convert_int.py`) once per component:

```bash
for COMP in text_encoder unet vae_decoder; do
  python skills/aipc-toolkit/scripts/aipc_convert_fp.py \
    --onnx        {ONNX_DIR}/${COMP}.onnx \
    --output-root {OUTPUT_DIR} \
    --precision   16 \
    --preserve-io-mode datatype \
    --target-arch {TARGET_ARCH}
done
```

> ⚠️ If `/tmp` is full, set `QAIRT_TMP_DIR` before the loop (see `qnn_conversion.md`
> §Temp Directory for Large Models).

---

## 6. Context Binary Generation — per component

Create a separate `.conf` / `.json` pair for each component (the `graph_names` field must
match the component name used during conversion):

```bash
SOC_ID=<soc_id>
DSP_ARCH=<dsp_arch>

for COMP in text_encoder unet vae_decoder; do
  CONF=/tmp/soc${SOC_ID}_${DSP_ARCH}_${COMP}.conf
  JSON=/tmp/soc${SOC_ID}_${DSP_ARCH}_${COMP}.json

  echo "{\"graphs\":[{\"graph_names\":[\"${COMP}\"],\"vtcm_mb\":0,\"O\":3}],\
\"devices\":[{\"soc_id\":${SOC_ID},\"dsp_arch\":\"${DSP_ARCH}\",\
\"cores\":[{\"perf_profile\":\"burst\",\"rpc_control_latency\":50}]}]}" > $CONF

  echo "{\"backend_extensions\":{\"shared_library_path\":\
\"$QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtpNetRunExtensions.so\",\
\"config_file_path\":\"$CONF\"}}" > $JSON

  $QAIRT_SDK_ROOT/bin/x86_64-linux-clang/qnn-context-binary-generator \
    --backend   $QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtp.so \
    --model     {OUTPUT_DIR}/test_libs_${COMP}_fp16_x86_64-linux-clang/x86_64-linux-clang/lib${COMP}.so \
    --binary_file lib${COMP}.so \
    --output_dir  {OUTPUT_DIR} \
    --config_file $JSON
done
```

---

## 7. Deployment — ONNX-match naming

The `onnxwrapper` discovers context binaries by appending suffixes to the `.onnx` path.
Deploy each context binary with the ONNX-matching name:

```bash
# Linux target
for COMP in text_encoder unet vae_decoder; do
  cp {OUTPUT_DIR}/lib${COMP}.so.bin {ONNX_DIR}/${COMP}.onnx.so.bin
done
```

> ⚠️ Always deploy the `.yaml` file alongside `.onnx` — the wrapper uses it to restore
> ONNX output order after HTP reorders tensors at compile time.

### Target disk space — deploy only what inference needs

Large ONNX files (FP32 inline weights) can be several GB each and will quickly fill
the target device's storage. The wrapper only needs the `.onnx` file to **resolve the
artifact path** — it does not load the ONNX weights at inference time (those come from
the context binary). For targets with limited disk space, deploy a **stub `.onnx`**
instead of the full file:

```bash
# On target: create zero-byte stub files (wrapper only needs the filename to exist)
for COMP in text_encoder unet vae_decoder; do
  touch <workdir>/onnx_models/${COMP}.onnx
done
```

**What must be on the target** (minimum required for inference):

| File | Required | Notes |
|---|---|---|
| `<comp>.onnx` | ✅ | Stub (0 bytes) is sufficient — wrapper uses it for path resolution |
| `<comp>.onnx.so.bin` | ✅ | Context binary — the actual model weights |
| `<comp>.yaml` | ✅ | I/O config — wrapper uses it to restore output tensor order |
| `<comp>.onnx.data` | ❌ | Not needed on target — weights are in the context binary |
| `lib<comp>.so` | ❌ | Not needed if context binary is present |

**Check target free space before deploying**:
```bash
ssh ubuntu@<target> 'df -h /'
# If < 2× total context binary size, clean up before deploying
```

**Minimal deploy command** (context binaries + YAMLs + stubs only):
```bash
# Copy context binaries and YAMLs
scp {ONNX_DIR}/*.onnx.so.bin {ONNX_DIR}/*.yaml ubuntu@<target>:<workdir>/onnx_models/

# Create stub .onnx files on target (no large file transfer needed)
ssh ubuntu@<target> 'for f in <workdir>/onnx_models/*.yaml; do
  touch "${f%.yaml}.onnx"; done'
```

---

## 8. Inference Script Structure

### 8.1 General pattern — session loading and chaining

A multi-component inference script loads each sub-model as a separate `onnxruntime.InferenceSession`
(intercepted by the `aipc` wrapper) and chains their outputs:

```python
import onnxruntime as ort

def load_session(name):
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    return ort.InferenceSession(f"onnx_models/{name}.onnx", sess_options=opts)

# Load all components once (outside the inference loop)
encoder_sess = load_session("encoder")
decoder_sess = load_session("decoder")

# Chain outputs: encoder output feeds decoder input
enc_out = encoder_sess.run(None, {"input": x})[0]
dec_out = decoder_sess.run(None, {"enc_hidden": enc_out})[0]
```

Run via the `aipc` wrapper so each session is routed through QNN HTP:

```bash
export QAI_QNN_LIBS_DIR=$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
export LD_LIBRARY_PATH=$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH
export ADSP_LIBRARY_PATH=$QAIRT_SDK_ROOT/lib/hexagon-v{DSP_ARCH}/unsigned
export QAI_QNN_RUNTIME=HTP

python aipc infer_{MODEL_NAME}.py
```

### 8.2 Fixed batch shapes and multiple forward passes

Models exported with `dynamic_axes=None` (fixed shapes) accept only the exact batch size
used during export. When the pipeline requires running the same model with different
inputs in the same step (e.g. two different conditioning vectors), run the model
**twice sequentially** rather than concatenating inputs into a larger batch:

```python
# Correct — two separate calls, each with batch=1
out_a = model_sess.run(None, {"input": x, "cond": cond_a})[0]
out_b = model_sess.run(None, {"input": x, "cond": cond_b})[0]
combined = combine(out_a, out_b)   # e.g. guidance: out_a + scale*(out_b - out_a)

# Wrong — batch=2 will fail if model was exported with batch=1
# out = model_sess.run(None, {"input": np.concatenate([x, x]),
#                              "cond":  np.concatenate([cond_a, cond_b])})[0]
```

This pattern applies to any model that needs conditional vs unconditional outputs,
positive vs negative prompts, or any other paired-input scheme.

### 8.3 Iterative loops with stateful schedulers

Many pipelines run a component in a loop (denoising, autoregressive decode, beam search).
The loop state (step schedule, scores, cache) must be implemented in **pure Python/numpy**
on the target if the original framework library (diffusers, transformers) is not installed.

**General approach**:

1. **Serialize loop state on the host** — extract the scheduler/loop parameters from the
   framework and save as `.npy` files alongside the ONNX models:

   ```python
   # On host — extract and save loop state
   import numpy as np
   # Example: diffusion scheduler
   np.save("onnx_models/schedule_alphas.npy",   scheduler.alphas_cumprod.numpy())
   np.save("onnx_models/schedule_timesteps.npy", scheduler.timesteps.numpy())
   # Example: autoregressive — save special token IDs
   np.save("onnx_models/special_tokens.npy", np.array([bos_id, eos_id, pad_id]))
   ```

2. **Validate the numpy implementation step-by-step** against the framework reference
   before deploying to the target:

   ```python
   # Compare numpy loop vs framework loop on the same inputs
   for i, t in enumerate(timesteps):
       out_ref   = framework_step(noise_pred, t, latents)
       out_numpy = numpy_step(noise_pred, int(t), latents)
       diff = abs(float(out_ref.mean()) - float(out_numpy.mean()))
       assert diff < 1e-5, f"Step {i}: mismatch {diff:.2e}"
   ```

   > ⚠️ Even small per-step errors compound over many iterations. A 0.1% error per step
   > becomes ~2% after 20 steps and ~10% after 100 steps. Validate **all** steps, not
   > just the first few.

3. **Load on target** — the `.npy` files are small and deploy alongside the ONNX stubs:

   ```python
   # On target — load pre-serialized loop state
   alphas    = np.load("onnx_models/schedule_alphas.npy")
   timesteps = np.load("onnx_models/schedule_timesteps.npy")
   ```

### 8.4 Safe exit for Linux ARM HTP

The AppBuilder teardown path can segfault on Linux ARM after inference completes
(see `references/troubleshooting.md` §AppBuilder teardown segfault). Add a safe-exit
guard at the end of every inference function:

```python
import os, sys

def run_pipeline(...):
    # ... run inference, save outputs ...
    result = postprocess(raw_output)

    # Safe-exit guard — bypass AppBuilder teardown segfault
    if os.getenv("AIPC_SAFE_EXIT", "0") == "1":
        sys.stdout.flush()
        os._exit(0)   # skips normal Python cleanup; save all outputs before this line
    return result
```

Run with:
```bash
export AIPC_SAFE_EXIT=1
python aipc infer_{MODEL_NAME}.py
```

> ⚠️ `os._exit(0)` skips all Python finalizers and `atexit` handlers. Always flush
> stdout and persist all outputs **before** this line.

### 8.5 Model-specific: diffusion pipelines (Stable Diffusion, SDXL)

The patterns below apply specifically to diffusion models that use a UNet denoiser with
classifier-free guidance (CFG). Skip this section for non-diffusion pipelines.

**CFG with fixed batch=1 UNet**

Diffusion UNets are typically exported with `batch=1` fixed shapes. CFG requires two
UNet passes per step (unconditional + conditional). Run them separately (§8.2 pattern):

```python
def denoise_step(unet_sess, latents, timestep, emb_neg, emb_pos, cfg_scale):
    ts = np.array([timestep], dtype=np.float32)
    noise_neg = unet_sess.run(["out_sample"],
        {"sample": latents, "timestep": ts,
         "encoder_hidden_states": emb_neg})[0]
    noise_pos = unet_sess.run(["out_sample"],
        {"sample": latents, "timestep": ts,
         "encoder_hidden_states": emb_pos})[0]
    return noise_neg + cfg_scale * (noise_pos - noise_neg)
```

**PLMS scheduler — portable numpy implementation**

Serialize the scheduler state from diffusers on the host (§8.3 pattern), then implement
the exact PLMS step in numpy. The implementation must be validated step-by-step against
the diffusers reference (diff < 1e-5 per step) before deploying.

Serialize on host:
```python
from diffusers import StableDiffusionPipeline
import numpy as np

pipe  = StableDiffusionPipeline.from_pretrained(MODEL_DIR, ...)
sched = pipe.scheduler
sched.set_timesteps(NUM_STEPS)
np.save("onnx_models/pndm_alphas_cumprod.npy",      sched.alphas_cumprod.numpy())
np.save(f"onnx_models/pndm_timesteps_{NUM_STEPS}.npy", sched.timesteps.numpy())
```

Numpy PLMS step (exact port of diffusers `PNDMScheduler`, `skip_prk_steps=True`):
```python
class PLMSScheduler:
    def __init__(self, acp, timesteps, num_train=1000, num_steps=20):
        self.acp = acp;  self.timesteps = timesteps
        self.final_acp = float(acp[0])   # set_alpha_to_one=False
        self.num_train = num_train;  self.num_steps = num_steps
        self.ets = [];  self.cur_sample = None;  self.counter = 0

    def _alpha(self, t):
        return self.final_acp if t < 0 else float(self.acp[t])

    def _get_prev_sample(self, sample, t, prev_t, mo):
        a_t = self._alpha(t);  a_p = self._alpha(prev_t)
        b_t = 1 - a_t;         b_p = 1 - a_p
        coeff = (a_p / a_t) ** 0.5
        denom = a_t * b_p**0.5 + (a_t * b_t * a_p)**0.5
        return (coeff * sample - (a_p - a_t) * mo / denom).astype(np.float32)

    def step(self, model_output, timestep, sample):
        prev_t = timestep - self.num_train // self.num_steps
        if self.counter != 1:
            self.ets = self.ets[-3:]; self.ets.append(model_output)
        else:
            prev_t = timestep; timestep = timestep + self.num_train // self.num_steps
        if   len(self.ets) == 1 and self.counter == 0:
            mo = model_output; self.cur_sample = sample
        elif len(self.ets) == 1 and self.counter == 1:
            mo = (model_output + self.ets[-1]) / 2
            sample = self.cur_sample; self.cur_sample = None
        elif len(self.ets) == 2: mo = (3*self.ets[-1] - self.ets[-2]) / 2
        elif len(self.ets) == 3: mo = (23*self.ets[-1] - 16*self.ets[-2] + 5*self.ets[-3]) / 12
        else:                    mo = (55*self.ets[-1] - 59*self.ets[-2] + 37*self.ets[-3] - 9*self.ets[-4]) / 24
        prev = self._get_prev_sample(sample, timestep, prev_t, mo)
        self.counter += 1
        return prev
```

**Latent sanity check after denoising**

Check the latent range before VAE decode. Diverged latents produce corrupted images:

```python
print(f"Latents: std={latents.std():.3f}  range=[{latents.min():.2f},{latents.max():.2f}]")
# Normal after denoising:  std ≈ 0.5–1.0,  abs(max) < 5
# Diverged (bad output):   std > 5  or  abs(max) > 10  → scheduler or UNet precision issue
```

**Noise output diagnostic checklist** — if the output image looks like noise or static:

| Check | How to verify | Fix |
|---|---|---|
| Scheduler type | `pipe.scheduler.config._class_name` | Use the correct scheduler class (DPMSolver++, DDIM, etc.) — never a hand-rolled Euler step |
| Timestep source | Print first 3 timesteps | Must come from `sched.set_timesteps(N)` → integers like `[999, 949, ...]`, not `np.linspace` |
| CFG applied | Check `guidance_scale` in pipeline config | Run uncond + cond passes; apply `noise = noise_uncond + scale*(noise_cond - noise_uncond)` |
| Output channels | Print `model_output.shape` | If `out_channels = 2 × latent_channels` (e.g. 8 vs 4), take only `[:, :latent_channels, ...]` |
| Std not decreasing | Print `latents.std()` each step | Should decrease from ~1.0 toward ~0.5–0.8; flat or rising std = wrong scheduler math |

**Learned sigma / doubled output channels**

Some diffusion transformers (e.g. PixArt-Sigma, DiT) output `2 × latent_channels` to
predict both noise and variance. The pipeline splits them before passing to the scheduler:

```python
# From diffusers PixArtSigmaPipeline.__call__:
if transformer.config.out_channels // 2 == latent_channels:
    noise_pred = noise_pred.chunk(2, dim=1)[0]   # take first half: noise only
```

Always check `model.config.out_channels` vs `model.config.in_channels` when inspecting
a new diffusion transformer. If `out_channels = 2 × in_channels`, apply this split.

**Scheduler on host, model calls on remote**

When the target device lacks the framework library (diffusers, transformers), run the
scheduler on the **host** and only send model forward passes to the remote:

```python
# Host: scheduler runs locally with diffusers
sched = DPMSolverMultistepScheduler(...)
sched.set_timesteps(num_steps)

for t in sched.timesteps:                          # integer timesteps from scheduler
    noise_pred = remote_model_call(latents, t)     # SSH + qnn-net-run to target
    latents = sched.step(noise_pred, t, latents,   # scheduler step on host CPU
                         return_dict=False)[0].numpy()
```

This avoids reimplementing complex multi-step schedulers (DPMSolver++, DEIS, etc.) in
numpy, which is error-prone. The scheduler state (alphas, step buffers) stays on the host.
Only the model weights and NPU execution live on the target.

---

## 9. `aipc_plan.md` checklist additions for multi-component projects

When `COMPONENTS` is set, replace every single-model task with a per-component loop:

| Single-model task | Multi-component equivalent |
|---|---|
| Export `{ONNX_FILE}` | Export each `{ONNX_DIR}/{COMP}.onnx` |
| Inspect `{ONNX_FILE}` | Inspect each component; record I/O per component |
| Dry-run `{ONNX_FILE}` | Dry-run each component; patch per component if needed |
| Convert `{ONNX_FILE}` | Convert each component; one output dir per component |
| Generate context binary | One `.conf`/`.json`/`.bin` per component |
| Deploy `{MODEL_NAME}.onnx.so.bin` | Deploy `{COMP}.onnx.so.bin` for each component |
| Validate inference | Run full pipeline end-to-end; validate final output |

---

## 10. Known Issues

| Issue | Cause | Fix |
|---|---|---|
| `StableDiffusionPipeline has no attribute 'eval'` | Pipeline is not `nn.Module` | Call `.eval()` on each sub-model, not the pipeline |
| `ValueError: protobuf too large (>2GB)` in `check_model` | UNet / large model | Use `onnx.checker.check_model("path.onnx")` not object |
| `onnxsim ValidationError: ir_version not set` | External-data model | Wrap `onnxsim` in `try/except`; skip on failure |
| `Failed to copy external data … permission or space issue` | `/tmp` full | Set `QAIRT_TMP_DIR` to a path with ≥2× model size free |
| `fastrpc memory map failed … error: 0x1` on large context binary | Context binary exceeds DSP SMMU window | Use JIT `.so` fallback or split model into smaller components |
| Target disk fills up during deployment | Full ONNX files (FP32 weights) are large; not needed on target | Deploy stub `.onnx` (0 bytes) + context binary + YAML only |
| `No space left on device` during `qnn-model-lib-generator` on host | `objcopy` needs ~2× `.bin` size free in workspace | Free stale intermediates (`.cpp`, `.bin`) or point build to larger volume |
