# Sub-Agent Dispatch Template (Model Hub)

> **When to read:** Main agent is about to dispatch a sub-agent to execute a model-hub task.
> Loaded on-demand from `SKILL.md § Sub-Agent Dispatch Template`.

---

## Why this exists

A sub-agent inherits **NOTHING** from the main agent's context — it only executes the prompt as written. Once (2026-06) a prompt polluted by `model-builder` paths told a sub-agent to search `C:\`/`C:\WoS_AI` for `.bin` → 30+ min hang (Issue 18). So when dispatching a sub-agent to this skill:

1. **Main agent MUST first `read` the full SKILL.md** (the one-line `Use for` catalog summary is not enough).
2. **Sub-agent prompt's first instruction MUST be "read the full SKILL.md before acting"** — sub-agent has blank context; all constraints (Issue 18, fixed-path download, `qai_appbuilder`) must be learned by reading.
3. **NEVER include any `model-builder` script/path/tool** (`run_pipeline.py`, `qnn-onnx-converter`, `qairt_sdk_root`, etc.) — those are for custom ONNX conversion, useless here and induce wrong searches.
4. **NEVER phrase locate-the-package as "search/look-for in `C:\`/`C:\Users`/`C:\WoS_AI`"** (Issue 18). Only fixed paths: `qai_hub` download / `curl` to `C:/WoS_AI/<model>/`.

---

## Reusable standard sub-agent prompt template

```
You will execute an model-hub skill task: <specific task, e.g. "download the Inception v3 pre-compiled package and run inference on a test image on the NPU, printing Top-5">.

[Step 1, mandatory] First use the read tool to fully read:
  ${APP_ROOT}\factory\chat_features\model-hub\SKILL.md
Follow it strictly (including the Pre-Flight Self-Check GATE, fixed-path download, Issue 18 anti-recursion, qai_appbuilder inference rule).
(${APP_ROOT} = QAIModelBuilder repo root; do not hardcode the absolute path of any particular machine.)

[Step 0.5, mandatory — NOTES.md two-step check]
The directory name under factory/chat_features/model-hub/models/ may differ from the model name in this prompt
(e.g. prompt says "inceptionv3" but directory is "inception_v3"; prompt says "BEiT" but directory is "beit").
NEVER guess the path or rely on a single glob — always do BOTH steps:
  1. list "factory/chat_features/model-hub/models/" to see the exact directory names present.
  2. read the matching NOTES.md using the exact name found in step 1.
If a matching NOTES.md exists it contains download links, I/O shapes, known issues, and a ready-made
inference script — use them directly and skip most of Steps 1~5.

[Locating the model package — fixed paths only, NEVER full-disk search]
- If qai_hub is installed (import qai_hub can print the version) → download directly with qai_hub; or per SKILL Step 1/3 use webfetch to get the link, curl download to C:/WoS_AI/<model>/.
- You may only check these two fixed shallow directories (Get-ChildItem WITHOUT -Recurse): C:\WoS_AI\<model>\, C:\Users\<user>\.qaihub\.
- 🚫 NEVER run -Recurse / glob **/* full-disk recursion on the UNBOUNDED roots C:\, C:\Users, or C:\WoS_AI WITHOUT a <model> subdir (it hangs for 30+ minutes). Recursing inside a specific bounded dir like C:\WoS_AI\<model>\ is fine.

[Environment] Read the inference venv from ${APP_ROOT}\data\config\qairt_env.json: `python_arm64_venv` on ARM64 hosts (windows-arm64 / linux-aarch64); `python_runtime_venv` on x64 hosts (windows-x64 / linux-x64). Inference always uses qai_appbuilder / QNNContext.
[Forbidden] Do not use any script/path from the model-builder skill (run_pipeline.py, qnn-onnx-converter, qairt conversion tools, etc.) — this task is a pre-compiled package, no conversion. (Exception: the shared exporter qai_pack_export.py referenced by SKILL Phase 7 is allowed — it is the common App Builder export chain, not a conversion tool.)

[MANDATORY after inference — Step 6.5 workspace normalization] Once inference is verified correct, you MUST run
  factory/chat_features/model-hub/scripts/aihub_to_manifest.py --workdir C:\WoS_AI\<model> --model-name <model> --precision <prec> --output-type <type>
to normalize the download into the App Builder contract (output/<model>_<label>.{bin,dlc} + inference_manifest.json). This is NOT optional — without it the model is invisible to App Builder's "Import" scan. --workdir must be the top-level C:\WoS_AI\<model> (not the nested ...-qnn_dlc-<prec> subfolder) and --model-name must equal that folder name.

When done, report: chipset detection result / model package path and contents / inference script path / Top-5 results / the normalized output/<model>_<label>.{bin,dlc} + inference_manifest.json paths (Step 6.5).
```
