# App Builder · Secondary Capabilities & Multi-Step Chain Rules

> **This file is a REFERENCE the Agent reads on demand.** The main `SKILL.md`
> keeps the **PRIMARY task** (generate a standalone fullstack app) plus hard
> disciplines inline. When the user asks for a **one-off** run / interpretation /
> chain / batch (rather than authoring a full app), read this file for the
> secondary-capability rules.

## Secondary capability: run, interpret, chain (assist / verify)

These are secondary — use them to VERIFY the WebUI you build or to assist a one-off request:

1. **Invoke model inference**: when the user requests processing of an
   image/audio/text (or you need to verify I/O shape), use the `appbuilder_run`
   tool to invoke the appropriate Model Pack.
   - If the task requires multi-step processing (e.g. "classify first, then
     super-resolve"), plan the call chain and execute it step by step.
   - When multiple models of the same category exist: prefer the one with status
     Ready that has the highest recorded historical quality score; if there is no
     score, choose based on the manifest description and explain your reasoning.
   - An output file path in a result (e.g. `data/outputs/r-xxx.png`) can be used
     directly as the input to the next model.
   - **Batch processing**: when the user asks to process multiple files (e.g. all
     images in a directory, a set of audio clips):
     - Prefer `appbuilder_batch_run` to submit all inputs at once (up to 20),
       avoiding repeated tool-calls that waste tokens and round trips.
     - When using the same model, include each file as one item of
       `batch[i].inputs`; optionally specify different `params` per item.
     - For "stop on error", pass `stopOnError: true`; the default false continues
       processing subsequent items and aggregates errors.
     - For more than 20 files, submit in batches (<= 20 each) and report progress
       to the user between batches.
     - A single file or a heterogeneous model chain still uses `appbuilder_run`.
2. **Interpret results**: for requests like "what are the key decisions in this
   transcript", "convert this table to Markdown", or "which brand names are in
   the image", produce summaries/extractions/reprocessing from the result JSON.
3. **Explain parameters**: answer parameter-semantics questions (e.g.
   `task=translate` vs `transcribe`, effect of `tile_size`) using the Pack's
   SKILL.md.
4. **Suggest alternatives**: when results are unsatisfactory (OCR misses,
   ASR misrecognitions), suggest parameter adjustments or switching models based
   on each Pack's limitations (within ASR there are two: Whisper / Zipformer).
5. **Assist with export**: when the user needs a specific format (Markdown table,
   SRT subtitles, JSON summary), use a tool (e.g. `write`) to save under
   `data/outputs/`.

## Multi-step chain invocation rules

When a task chains multiple models (e.g. classify then super-resolve, ASR then
TTS):

1. After the first `appbuilder_run` call, extract the output path from the result
   text (`Output image: data/outputs/r-xxx.png` / `Output audio:
   data/outputs/r-xxx.wav`); the result text already annotates that "this path
   can be used as the `inputs.image` / `inputs.audio` of the next
   `appbuilder_run` call".
2. Pass that relative path directly as the `inputs` of the next `appbuilder_run`,
   without any path conversion.
3. Intermediate artifact paths look like `data/outputs/r-xxxxxxxxxxxx.png` (the
   `r-` prefix comes from `runner.new_run_id()`) and can be referenced directly.
4. Inference on the NPU is serial; multiple calls are queued automatically
   (sharing `_npu_lock`), with no need to wait manually or add a sleep.
5. Before and after a call, verbally tell the user which step of the chain this
   is, which model this step used, and what intermediate artifact was produced,
   so the user can track progress.

Batch guidance: prefer `appbuilder_batch_run` (up to 20 inputs at once); pass
`stopOnError: true` to halt on the first failure (default false aggregates
errors); for more than 20 files, submit in batches of <= 20 and report progress
between batches.

Example chains (short):

- **Image classify -> conditional super-resolve**: `inception-v3` classify ->
  if `predictions[0].label` is a category of interest, call `real-esrgan-x4plus`
  on the same original image for 4x super-resolution; otherwise ask the user
  before proceeding.
- **Speech ASR -> TTS**: `whisper-base` transcribe `data/uploads/audio/xxx.wav`
  to get `fullText` (translate the text in plain conversation if needed), then
  `melotts-zh` synthesize the text via `inputs={"text": "..."}` to produce
  `data/outputs/r-xxx.wav`.
