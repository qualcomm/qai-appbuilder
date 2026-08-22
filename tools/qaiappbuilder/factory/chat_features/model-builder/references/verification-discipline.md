# Verification Discipline: Three Iron Rules (cross-domain — conversion / accuracy / performance)

> **Knowledge scope**: the three highest-leverage meta-rules that save time during accuracy and performance work — cheap falsification before expensive rebuilds, the compiled artifact is the source of truth for the contract, "host passes ≠ device passes". Kept under `references/` because these rules span conversion / verification and are not exclusive to any single sub-capability.
> **Kernel**: (1) minute-level falsification before expensive rebuild (one rebuild + device run ≈ 1.5 h; one numpy round-trip check takes a few minutes); (2) compiled-artifact (`.bin` / DLC) metadata is the source of truth, ranking above the export code (the two drift apart); (3) host metric passing ≠ device passing, plus a layered attribution ladder, plus the pattern of feeding a structured trace to an LLM to ask where the bottleneck is.
> **Shell stripped**: the LLM-specific wrappers of one LLM-optimization pipeline (PPL / KV-cache / autoregression / C++ runner) are dropped; a certain Android trace-analysis tool itself does not apply here, only its "LLM + structured trace → bottleneck question" pattern is absorbed. This document mutually reinforces AGENTS.md §5 State-Truth-First and §4 "defects found must be fixed".

## Contents
- Iron rule 1: minute-level falsification before expensive rebuild
- Iron rule 2: compiled-artifact metadata is the source of truth
- Iron rule 3: host passing ≠ device passing + layered attribution + AI reads the trace

---

## Iron rule 1: minute-level falsification before expensive rebuild

**Every "raising precision / changing the config will fix this" hypothesis must first be checked against a few-minute cheap probe before you commit ~30 min of rebuild + ~60 min of device validation.**

### The flagship "5-minute killer check": re-quantize to the device's scale, then measure cosine

> Re-quantize the FP32 reference tensor with the scale / offset the device actually uses, and measure its cosine against the original FP32. If cos ≈ 0.999, the quantization grid itself is near-lossless — in that case "raise precision / widen the bit-width" cannot help at all, and the whole route can be cut.

Logic: if, after re-quantizing with the device's real scale, cosine to FP32 is already ≈0.999, the **representational precision of this tensor is not the bottleneck**. Piling on FP16 / INT16 is just churning on an already-fine grid (FP16's ~10-bit mantissa is even coarser than uint16's 65536-level grid, and can actually be worse). Generic form (a user's own small ONNX model): when a layer is suspected of insufficient precision, first round-trip the layer's FP32 reference tensor through the device's quantization parameters in numpy on the host and measure cos-to-FP32; if ≈0.999, do not invest a rebuild on "representational precision".

Other minute-level falsification patterns: run a single block / single step instead of the whole thing; do a file-level dissection (see rule 2) instead of re-exporting; do a static check (shape / dtype / scale correct?) instead of going on-device.

**Rule wording**: every hypothesis must be paired first with a minute-level check (file dissection / single block / numpy re-quant / static) to confirm or falsify it, and only then is one expensive rebuild + device validation allowed.

## Iron rule 2: compiled-artifact metadata is the source of truth, above the export code

**I/O contract, per-tensor scale / offset, dtype, shape — read all of these from the compiled artifact (`.bin` / DLC), never re-derive them from the export script / ONNX export code — the two drift apart.**

- "Re-deriving" quantization parameters from the export script assumes "the params at export time == the params baked into the deployment artifact" — **that assumption is often wrong**: the quantization toolchain may recalibrate at compile time and rewrite scale / bit-width, so the numbers in the export code will not match what is burned into the `.bin`.
- **The artifact is what the device actually runs**, so its metadata = ground truth; analysing the artifact carries the same evidentiary weight as one on-device run.

How to inspect it (available on WoS PCs):
- `qnn-context-binary-utility --context_binary <bin> --json_file <out>.json` — dumps every tensor's `name` / `dataType` / `dimensions` / `quantizeParams.scaleOffset`.
- DLC → JSON (e.g. `qairt-dlc-to-json`) — dumps per-tensor scale / offset / dtype so you can confirm whether the quantization tool **respected** the encodings you provided (or recalibrated / drifted), and whether the host-side encoding **actually reached the device**.
- **Read the dequantization formula from the artifact too**: `fp32 = (quant + offset) * scale` — scale / offset must be fetched from the JSON on demand; never hard-code them.

Cross-check with AGENTS.md §5: the single source of truth for the contract is the compiled artifact; the export script is only one (potentially drifted) upstream of it.

## Iron rule 3: host metric passing ≠ device passing; use AI to read the trace

### Host passing does not imply device passing

Host-side simulation frequently **does not model the device's fixed-point accumulator / fixed-point compute path** — inside operators it computes in float and only inserts quantization at tensor boundaries. There is therefore a **structural difference** between host and device, and "device == host simulation" may not be reachable at all. Reporting discipline:
- Accuracy / performance numbers **must be measured on the device**; you cannot use a host number as the device conclusion.
- Compare against a **same-protocol, same-batch** host reference.
- When the device is much worse than the host, first suspect that "the reference itself is not faithful" (the host did not model fixed-point accumulation) rather than immediately blaming a device bug; if necessary, redefine the reference (inject device-side fixed-point behaviour into the host reference) before the comparison becomes meaningful.

### Layered attribution ladder (when device disagrees with host — cheap steps first, expensive later)

1. **Dissect the real artifact (zero device cost)**: read per-tensor scale / offset / dtype from the `.bin` / DLC and confirm whether the toolchain drifted.
2. **Three-way step comparison (FP32 / host-side quantized simulation / device)** on the same batch, so that "ordinary quantization loss (also visible in simulation)" is separated from "device-specific difference" — comparing only device-vs-FP32 conflates the two.
3. **Step-wise / layer-wise trend**: along the computation chain, compare cosine of device-vs-FP32 and sim-vs-FP32 step by step; if the device curve drops with step count while sim stays flat, the drift is device-side (accumulator / state).
4. **Device-side intermediate-tensor dump (real-device locator)**: dump device-side intermediate tensors (dequantized with the tensor's own encoding) and compare layer- / channel-wise against host FP32; **the dump must be strictly incremental — verify the main output is bit-identical before and after the dump (md5 check)**, and only send back the small intermediate tensors.
5. **Cheap re-quantization upper bound (numpy, minute-level)**: the 5-minute killer check from rule 1 — run this before any rebuild.

### AI trace-analysis pattern

Feed a structured profiling trace (e.g. `chromeTrace.json`) to an LLM and ask, in natural language, "where is the bottleneck / which stage takes the most time / where does it stall?" This complements the existing profiling analysis tooling.

## Relation to existing SKILLs

The existing profiling docs cover how to collect a trace; the accuracy-side docs cover how to find which layer is off. This reference adds the **verification working principle that runs through both** (cheap falsification before expensive rebuild, contract-from-artifact, host ≠ device + layered attribution + AI-read trace) — a methodological discipline that complements, and does not duplicate, the concrete collection / localization steps.
