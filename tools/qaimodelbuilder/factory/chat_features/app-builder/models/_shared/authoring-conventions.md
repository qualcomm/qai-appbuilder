# App Builder Model Pack SKILL — Shared Authoring Conventions

> **Purpose**: Every Model Pack SKILL under `factory/chat_features/app-builder/models/<id>/SKILL.md`
> follows the same 8-section skeleton and shares the same generic disciplines.
> This file captures the **generic parts** so each Pack SKILL can focus on its
> **model-specific content slots** (fields, parameters, use cases, limitations,
> examples) without repeating boilerplate. Pack SKILLs reference this file with
> a one-line pointer, then fill in their own specifics.

## Standard 8-section skeleton

Every Pack SKILL uses this structure (numbering and titles):

1. **What this Pack does** — one-liner + on-device NPU note
2. **Parameters** — what the user can tune before Running
3. **Output JSON Schema (canonical contract)** — every produced field, one subsection each
4. **(For non-trivial models)** Frontend rewrites / language-specific quirks — set user expectations
5. **Typical user requests and how to handle them** — 5–6 numbered use cases
6. **Known limitations** — be honest with the user
7. **(Optional) Sibling comparison** — e.g. whisper-base vs zipformer-zh, when to redirect
8. **What you (the LLM) should NOT do** — generic disciplines below + Pack-specific field list
9. **Quick reference — example output** — concrete JSON blob

## Section 3: "canonical contract" — writing rules

- Title MUST be exactly: `## 3. Output JSON Schema (canonical contract)`. The
  word "canonical contract" signals to the LLM that the schema is fixed and
  MUST NOT be invented / extended.
- Every top-level field gets its own `### 3.x <field>` subsection listing:
  type, meaning, unit (for numbers), value range or enum, and — critically —
  what the field is **NOT** (to prevent hallucination).
- Enumerate every field the runner actually produces. If a field is absent from
  the schema, it is absent from the output; do not describe hypothetical fields.

## Section 8: "should NOT do" — generic disciplines (shared across all Packs)

These four disciplines apply to **every** App Builder Model Pack. Each Pack SKILL
copies them verbatim, then appends its own **Pack-specific "do not invent" field
list** and any **Pack-specific behavioural constraints**.

- **Do not re-run just to interpret an existing result.** If a Run result is
  already in your context, interpret it rather than re-running. You MAY call
  `appbuilder_run` to verify I/O when building a WebUI, but re-running is the
  user's job (they click Run again).
- **Do NOT MODIFY** these files (developer-maintained). You MAY `read`
  `runner.py` READ-ONLY to understand the model's input/output when building a
  WebUI. Run inference via the HTTP API / the `appbuilder_run` tool — do not
  execute `runner.py` inside the generated app.
- **Do not invent fields** that aren't in the schema (see the Pack's §3 for the
  exact allowed field list). Stick to what §3 declares.
- **Do not promise capabilities** that this specific Pack does not implement
  (voices/emotion/dialect for TTS; specific-language-only ASR; specific fonts
  or layouts for OCR; particular tile sizes for SR). Each Pack lists its own
  concrete "does not exist" claims — do not extrapolate.

## Section 5: "typical user requests" — writing rules

- Each subsection = one realistic user quote in scare quotes ("Read this aloud",
  "Extract all monetary amounts", …), followed by concrete steps the LLM takes.
- Prefer 5–6 subsections per Pack. Cover: the primary happy-path, at least one
  export/format request, at least one "why does it behave this way?" question,
  and at least one "please switch to a different model" redirect where relevant.

## Section 6: "Known limitations" — writing rules

- Group by category: input constraints, quality/accuracy edges, language/dialect
  boundaries, format not supported, latency reality on target hardware.
- Include the **positive framing** where possible ("works well on X, degrades on
  Y") so the LLM can set correct expectations rather than blanket refusals.

## Section 9: Quick reference — example output

- Include a full concrete JSON output blob (5–20 lines) that matches §3 exactly.
- The LLM uses this as an anchor when generating summaries / SRT / Markdown /
  interpretation — it is faster than re-inferring the schema.

---

## How to reference this file from a Pack SKILL

At the top of your Pack SKILL, near section 8, replace the generic disciplines
with a one-line pointer:

```
## <N>. What you (the LLM) should NOT do

Generic disciplines (re-run avoidance, do-not-modify, do-not-invent-fields,
do-not-promise-capabilities) → `../_shared/authoring-conventions.md § Section 8`.

**Pack-specific additions** (this Pack only):

- Do not invent fields not in §3 — stick to `<field1>`, `<field2>`, `<field3>`, …
- Do not promise <capabilities-this-Pack-lacks> — none of these exist in this Pack.
- <other Pack-specific behavioural rules>
```

The Pack-specific field list and capability claims stay in each Pack SKILL —
they are real differences, not boilerplate. Only the generic wording is shared.
