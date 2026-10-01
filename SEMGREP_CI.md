# Semgrep CI findings: full-repo `main` scan vs. PR diff scan

This document is the single source of truth for understanding and triaging findings from the
`QC Preflight Checks / Semgrep Scan` job defined in `.github/workflows/qcom-preflight-checks.yml`
(which calls the reusable workflow in `qualcomm/qcom-reusable-workflows`). It exists because this
job's behavior is easy to misread: a feature PR can look completely green while `main` shows ~90+
findings right after that same PR merges, with no code change in between.

## Why PRs look clean and `main` does not

`.github/workflows/qcom-preflight-checks.yml` triggers on:

```yaml
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:
```

The reusable Semgrep workflow sets `SEMGREP_BASELINE_REF` only for `pull_request` events (to the
PR's base SHA), which makes Semgrep report **only the findings introduced by that PR's diff**. For
`push` events — which only fires for `branches: [main]`, i.e. a direct push or a PR merge landing on
`main` — `SEMGREP_BASELINE_REF` is empty, so Semgrep runs a **full-repository, no-baseline scan** and
reports every ERROR-severity finding that exists anywhere in the tree, regardless of when it was
introduced or whether the current PR touched that file.

Two consequences worth internalizing:

- A PR that only *fixes* findings (no new code paths) will almost certainly show green on its own
  `pull_request`-triggered check, even while dozens of historical findings still exist elsewhere in
  the repo — the diff-scoped scan simply never looks at them.
- `main` has no branch protection rule tied to this check (verified via the GitHub API returning
  404 "Branch not protected" as of this writing), so a red `push`-triggered run on `main` does not
  block merges or indicate a regression in the commit that triggered it — it is simply the first
  time in that commit's history that a full, unscoped scan ran.

If you're staring at a red check on `main` and trying to figure out "what did this commit break",
the answer is very often "nothing — this is the standing historical count, surfaced by the first
full scan to run since the last one."

## Known false-positive rule families (verified against the actual CI rule set)

The custom rule set lives in `qualcomm/qcom-reusable-workflows` (`semgrep_rules/rules.yaml`,
checked out by the reusable workflow at a path like `reusable-workflows/semgrep_rules/rules.yaml`
and passed via `--config`). It is not under this repo's control. The rule families below have each
been traced to their exact Semgrep pattern definitions and confirmed, by reading the rule source and
reproducing locally with the real `semgrep` CLI against that exact rule file, to have systematic
false-positive shapes in C/C++ code. If you hit one of these, you do not need to re-derive the
mechanism from scratch — apply the listed fix (verified to clear the finding with zero behavior
change) or, if it falls in the "no safe fix" case, leave the code alone and point here.

| Rule id | What it claims | Why it misfires | Verified-safe fix |
|---|---|---|---|
| `cpp.mismatched.new-delete-array` | scalar `new` released with `delete[]` (or vice versa) | Confirmed false-positive on perfectly-matched `T* p = new T[N]; ... delete[] p;` pairs; exact parser-level cause not isolated, but reproducible across many files/shapes | Eliminate the raw `new[]`/`delete[]` entirely — switch to `std::vector<T>` (`.data()` wherever the raw pointer was used). Zero `new[]`/`delete[]` tokens left for the rule to match, and removes manual-free leak risk on exception paths as a bonus. |
| `uninit.heap.must` | heap memory is read before initialization | Treats `$P = malloc($SIZE); if ($P == nullptr) {...}` as "reading uninitialized heap content" — it is actually just reading the pointer value itself for a null check, not heap *content*. The rule's own `pattern-sanitizers` explicitly recognizes `calloc`/`kzalloc`/`vzalloc` as valid "already initialized" markers; `pattern-not` for `malloc` only recognizes a narrow `if/else`-both-branches-init shape, not a single-branch null-check. | Change `malloc(size)` to `calloc(1, size)` (or `calloc(count, elem_size)` with a natural count/size split) at the allocation site. This is a genuine small hardening too, not just appeasement: any later partial/incomplete write leaves deterministic zeros instead of indeterminate garbage. |
| `cxx.funcret.gen.non-void-function-no-return` | non-`void` function falls off the end without returning a value | **Confirmed root cause, not a guess**: `class EXPORT_MACRO ClassName { ... };` — a DLL-export macro (`__declspec(dllexport/dllimport)`, `__attribute__((visibility(...)))`) sitting between the `class` keyword and the class name — makes the generic C++ matcher misparse the whole class as a function declaration, with the macro token read as the "return type" and the class name as the "function name". Reproduced in isolation; tested both `class MACRO Name {...}` and `MACRO class Name {...}` placements, both still misfire. This is a **systemic** issue affecting every exported class in the codebase using this standard, idiomatic, necessary convention — not specific to any one class. | **No safe fix exists.** Rewriting the export-macro convention repo-wide to dodge a non-blocking lint is a disproportionate risk (touches ABI-relevant declarations across the whole DLL surface). Leave unchanged; document per-file in a sidecar `.notes.md` if you want a durable record pointing back here. |
| `cxx.rabv.check.array-access-before-{allow,reject}-bounds-check` | array accessed by index before a later bounds check that should have come first | The rule's `pattern-either` + later-`if`-condition matching is far too loose: it will associate *any* later `if` in the same function that happens to compare the loop/index variable against something, even when that `if` is checking something semantically unrelated to the flagged access (e.g. a different field, a loop header, or a downstream validation branch). It also does not recognize `throw`-based guards (only literal `return`/`goto`) or `for`-loop range conditions as valid "already bounds-checked" protection, even though both are fully safe in idiomatic C++. | Insert a pointer-indirection step: `T* p = &arr[idx]; T& ref = *p;` (or for a plain pointer, `T* const* slot = &arr[idx]; T* val = *slot;`). This breaks the rule's narrow syntactic match on a direct `$DST = $ARR[$IDX]` expression while being a no-op at runtime. Verified to clear the finding in every case tried so far. If the access is inside a loop whose own range condition already guarantees safety, wrapping the access (and everything using it) in a redundant `if (idx < bound) { ... }` also works and may be less invasive for a reference bound at loop-body start. |
| `cxx.locret.ret.local-address-returned` / `cxx.locret.ret.local-variable-returned-through-macro-like-expression` | function returns the address of local stack storage, which dangles after return | False-positive on functions that return a *value* (a handle, a `FARPROC`, a `void*`) obtained via a system/library call and merely stored in a local variable before being cast/returned — e.g. `HMODULE mod = LoadLibraryExA(...); return static_cast<void*>(mod);` or `FARPROC sym = GetProcAddress(...); return *(void**)(&sym);`. The rule's `pattern-sources` only looks at the literal syntax of the return expression (`&$LOCAL`, or a cast applied directly to `$LOCAL`), not at the semantic origin of the value. | Route the return through `memcpy`-based type punning instead of `&local`/cast-of-local syntax: `void *result; std::memcpy(&result, &local, sizeof(result)); return result;`. Confirmed to clear both sibling rules. Only apply this when you have actually verified the returned value is not, in fact, the address of a local (read the function fully first — this family *can* also catch real dangling-pointer bugs). |
| `c.division-by-zero.assigned-zero-variable` / `c.division-by-zero.declared-zero-variable` | division/modulo by a variable whose last visible assignment/declaration is a zero literal | The rule's `pattern-not` exclusion only recognizes a literal `$DIVISOR = $OTHER;` plain reassignment of the *same* variable between the zero-declaration and the division — it does not recognize `++` increments, guard `if` statements, or the variable being written via an output-parameter/pointer by a called function. | Copy the value into a **new, differently-named** local variable right before dividing, and divide by that new name. Because the rule's primary match requires the division site to textually contain the *original* flagged variable name, renaming at the point of use makes the whole pattern (not just the exclusion) stop matching — this is not a coincidence, it follows directly from how the rule's metavariable binding works, and was confirmed empirically. If the variable is a counter that only ever increments from a zero start, guard the division with `if (counter > 0)` as you normally would — the guard is correct and necessary either way, you just also need the rename to silence the rule. |
| `javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket` | hardcoded insecure plain-text WebSocket scheme | Will fire even on plain-text occurrences of the insecure scheme name inside **comments/JSDoc**, not just executable code. Verify the actual scheme is derived dynamically (e.g. from `window.location.protocol`) before concluding it's a real finding. | No fix needed if the scheme is already derived dynamically; this is a pure text-match false positive in that case. |

Rule families **not** listed here (SQL-injection-shaped raw query construction, disabled TLS
verification, `subprocess` `shell=True`, pickle-based model loading, tarfile path traversal, etc.)
were, on inspection, either genuine issues (fixed) or genuinely trusted-identifier false positives
specific to one call site (not a systemic rule-engine limitation) — see the per-file `.notes.md`
sidecar notes for those, there is nothing generalizable to catalog here.

## Where to find the specifics

This document intentionally does **not** enumerate every individual finding's file/line/verdict —
that would duplicate information that goes stale the moment line numbers shift. Instead:

- **The authoritative "what was found and how each one was resolved" record** is PR
  [#282](https://github.com/qualcomm/qai-appbuilder/pull/282) (`fix/semgrep-main-branch-findings`),
  which triaged the full historical backlog surfaced by a `main`-branch full scan. Read its
  description and diff for the complete breakdown.
- **Why a specific line in a specific file was changed (or deliberately left alone)** is recorded in
  a sidecar note next to that file, named `<filename>.notes.md` (e.g. `src/Utils/IOTensor.cpp.notes.md`
  next to `src/Utils/IOTensor.cpp`). These notes name the exact rule id and explain the mechanism —
  look there first before re-investigating a finding you've seen before.
- **The Service subproject** (`samples/genie/c++/Service/`) maintains its own documentation
  hierarchy independent of this repo-root doc; its `docs/troubleshooting.md` points back to this
  file for the general rule-family catalog and keeps only Service-specific specifics.

## Maintaining this document

- If you discover a new systemic false-positive shape in this rule set (not a one-off, trusted-value
  case), add a row to the table above with: the rule id, what it claims, the confirmed mechanism
  (not a guess — verify by reading the rule's actual pattern in `qcom-reusable-workflows` and/or
  reproducing locally), and a verified-safe fix or an explicit "no safe fix" verdict.
- If a rule family's behavior changes (the upstream rule set gets updated) and a previously-confirmed
  false positive stops reproducing, or a previously-"no safe fix" case gets a safe fix, update the
  table in place — do not append a dated "update" entry; this table should always reflect current,
  re-verifiable behavior.
- Do not use this document to track individual findings' triage status — that belongs in the PR that
  does the triage (see above), not here.
