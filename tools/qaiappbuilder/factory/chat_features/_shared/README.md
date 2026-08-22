# _shared — cross-skill shared references

This directory holds knowledge that **multiple skills reference** — a single
source of truth. `app-builder` / `model-builder` / `model-hub` refer to files
here using the `${APP_ROOT}` variable path; **do NOT copy the body into
individual skills**, or they will drift out of sync.

> ⚠️ **Path convention:** No absolute paths anywhere in the project (install
> location differs per machine). All cross-directory references use the
> `${APP_ROOT}/factory/chat_features/...` variable placeholder, resolved by the
> host at runtime.

## Files

| File | Content | Referenced by |
|------|---------|---------------|
| `qnn-inference-routing.md` | Per-platform inference routing (defaults + user override triggers); terminology; `.bin` producer/consumer binding; short pointers to `x64-host-notes.md` for x64 local-inference rules, B11, closing statement | app-builder, model-builder, model-hub |
| `x64-host-notes.md` | x64 host local-inference guide: when to read; compatibility matrix; backend selection via the `question` tool; `B11` blocking condition; MANDATORY closing statement template; common error signals | app-builder, model-builder, model-hub |

## How to reference

Add one identical line in each skill's routing / topic area:
`${APP_ROOT}/factory/chat_features/_shared/qnn-inference-routing.md`
