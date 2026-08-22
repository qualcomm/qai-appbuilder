# Pack SKILL Template

> Copy this file to `features/app-builder/models/<your-pack>/SKILL.md` and fill it in following the outline below.
> This file is injected into the LLM only when manifest.skill.enabled=true, during `Send to Chat` / app-builder mode conversations.

## Model Overview

One sentence: what the model does + what it takes as input + what it produces as output.

## Key Parameters

| Parameter | Meaning | Recommended Value | When to Adjust |
|------|------|-------|---------|
| ... | ... | ... | ... |

## Output Schema Field Meanings

```json
{
  "field_a": "...",
  "field_b": "..."
}
```

- `field_a`: ...
- `field_b`: ...

## Typical Conversation Scenarios

- User: "..."
- You should: "..."

## Known Boundaries / Weaknesses

- ...
- ...

## What You Do **Not** Do

- Do not call the runner directly via `exec` (inference is handled by the front end's `/api/appbuilder/run`).
- Do not modify the Pack's manifest.json / runner.py / weights.
