# Universal Diagnosis Framework

> **Knowledge scope:** The shared opening framework for every domain-specific troubleshooting sub-SKILL (conversion / inference / accuracy / performance / stability / …) — defines *how* to diagnose; each domain SKILL defines the symptoms and fixes for that domain.
> **Distilled core:** Four phases (root-cause investigation → pattern analysis → single-hypothesis minimal verification → fix the root cause, not the symptom), three iron laws, reverse tracing (trace back along the call chain to the original trigger point), defense-in-depth, red-flag checklist, and rationalization table.
> **Abstracted away:** Internal skill-name references from the source methodology framework; language-specific examples rewritten as language/domain-neutral pseudocode skeletons.
> **Note:** This file's `_` prefix marks it as a "shared framework / non-standalone-triggerable SKILL" — it has **no YAML front-matter** and will not be loaded as an independent SKILL by the skill discovery system. Each troubleshooting sub-SKILL references it at the top.

## Core Principle

Random trial-and-error wastes time and creates new bugs; patching symptoms only hides the underlying problem.

**Core Principle: always find the root cause before touching anything. Fixing symptoms = failure. Violating the letter of this process is violating the spirit of diagnosis.**

## The Iron Law

```
No fix may be proposed until root-cause investigation is complete.
```

## When to apply

Any technical problem: failing tests, production bugs, unexpected behaviour, performance issues, build failures, integration failures, API error codes … **especially** when under time pressure, when "just one small change" looks obvious, when several fixes have already been tried, when the last fix did not work, or when you do not fully understand the problem. Do NOT skip the process because "the problem looks simple / we're in a hurry / management wants it fixed now."

---

## Three Iron Laws (apply throughout — operationalize these)

1. **No fix until root-cause investigation is complete** (= no proposals until Phase 1 is done).
2. **Change one variable at a time** (minimal verification; changing multiple things at once makes it impossible to isolate what worked and introduces new bugs).
3. **Same problem fixed 3 times and still broken = architectural problem — stop and report.** Do not blindly attempt a 4th fix.

---

## Four Phases (must complete each phase in order — no skipping)

### Phase 1: Root-Cause Investigation (before touching anything)

1. **Read the error message carefully:** do not skip errors/warnings — they often contain the answer directly; read the full stack; note line numbers / paths / error codes.
2. **Reproduce reliably:** can it be triggered consistently? Exact steps? Does it happen every time? Not reproducible → collect more data, do not guess.
3. **Review recent changes:** what change could have caused this? `git diff` / recent commits / new dependencies / config changes / environment differences.
4. **Instrument for evidence in multi-component systems:** before proposing a fix, add diagnostic instrumentation at every component boundary — log data entering/leaving each component, verify that environment/config is correctly propagated, check state at each layer; run once to see "which layer breaks", then drill into only that component.
5. **Reverse-trace the data flow** (when the error is deep in the call stack): trace back along the call chain to the **source** of the bad value; fix at the source, not at the symptom site (see §Reverse Tracing below).

### Phase 2: Pattern Analysis (find patterns before fixing)

1. **Find a working positive example:** similar code in the same codebase that works.
2. **Compare against the reference implementation:** if applying a pattern, **read the reference implementation in full** — do not just skim it.
3. **List every difference:** every discrepancy between working and broken, no matter how small.
4. **Understand dependencies:** what other components / config / environment does it require? What assumptions does it make?

### Phase 3: Hypothesis and Testing (scientific method)

1. **Form a single hypothesis:** write down explicitly "I believe the root cause is X, because Y" — specific, unambiguous.
2. **Minimal verification:** make **the smallest change that can verify this hypothesis** — one variable at a time.
3. **Verify before proceeding:** worked → Phase 4; did not work → form a **new** hypothesis; do **not** stack more fixes on top of the old one.
4. **Admit uncertainty:** if you don't know, say so — look it up or ask; do not pretend to know.

### Phase 4: Implementation (fix the root cause, not the symptom)

1. **Write a failing test case first:** the simplest reproduction (automated test preferred) — **must exist before the fix**.
2. **Single fix only:** target the identified root cause with one change; no "while I'm here" improvements; no bundled refactors.
3. **Verify the fix:** is it tested now? Are other tests still passing? Is the problem actually solved?
4. **When the fix does not work:** **stop** and count the attempts — <3 return to Phase 1 with new information; **≥3 stop and question the architecture**.
5. **3+ failures = question the architecture:** signals are each fix revealing new shared state / coupling in a different place; each requiring a large refactor to land; each creating new symptoms elsewhere. Stop and question whether the fundamental pattern holds — **discuss with others before attempting more fixes**.

---

## Reverse Tracing (Root-Cause Tracing)

Bugs often surface deep in the call stack; the instinct is to fix at the error site, but that treats symptoms. Trace **backwards** along the call chain to the original trigger point, and fix there: ① observe the symptom; ② find the immediate cause; ③ ask "who called it?" — list the call chain upward level by level; ④ continue upward to see what value was passed in; ⑤ find where that bad value was first produced, and fix there. When manual tracing is impractical, add logging **before** the dangerous operation (not after the failure), capturing context (key parameters, current working directory / state, environment variables, timestamp) and the full call stack. **Principle: never fix only at the point where the error surfaces.**

---

## Defense-in-Depth — after finding the root cause, make the bug structurally impossible to recur

Adding a check at only one place can be bypassed by different code paths / refactors / mocks. **Add checks at every layer the data flows through.** Four-layer template: ① entry-point validation (reject obviously invalid input at API boundary); ② business-logic validation (data makes sense for this operation); ③ environment guard (block dangerous operations in specific contexts); ④ debug instrumentation (log context + call stack before dangerous operations). Pairs with "fix the root cause": after finding one root cause, systematically scan for the same class of problem and harden every layer.

---

## Red-Flag Checklist — stop and return to Phase 1 if any of these apply

If you find yourself thinking: "quick fix now, investigate later" / "try changing X and see" / "add multiple changes and run tests" / "skip tests, I'll verify manually" / "probably X, I'll fix that first" / "I don't fully understand this but maybe it'll work" / "the pattern says X but I'll adapt it differently" / proposing a fix before tracing the data flow / **"one last fix" (when already 2+ attempts have failed)** / each fix revealing a new problem in a different place — **all of these mean: stop, return to Phase 1**. 3+ failures → question the architecture (Phase 4.5).

## Rationalization Table

| Excuse | Reality |
|--------|---------|
| "Problem is simple, no need for the process" | Simple problems have root causes too; the process is fast for simple bugs. |
| "Urgent, no time for the process" | Systematic diagnosis is faster than guess-and-crash. |
| "Try this first, investigate later" | The first fix sets the tone — get it right from the start. |
| "Add tests after confirming the fix works" | Untested fixes are fragile; write the test first to prove it. |
| "Fix multiple things at once to save time" | Cannot isolate what worked; introduces new bugs. |
| "Reference is too long, I'll adapt the pattern" | Half-understanding causes bugs — read it in full. |
| "I can see the problem, fixing directly" | Seeing the symptom ≠ understanding the root cause. |
| "One last fix" (already 2+ failures) | 3+ failures = architectural problem — question the pattern, stop fixing. |

## Quick Reference

| Phase | Key actions | Exit criterion |
|-------|-------------|----------------|
| **1. Root cause** | Read error, reproduce, review changes, instrument for evidence, reverse-trace | Know WHAT and WHY |
| **2. Pattern** | Find working example, compare, list differences | Differences identified |
| **3. Hypothesis** | Form single hypothesis, minimal verification | Confirmed or new hypothesis formed |
| **4. Implement** | Write failing test, single fix, verify, defense-in-depth | Bug resolved, tests pass, structurally prevented |

> **When "no root cause is found":** if systematic investigation genuinely indicates the problem is environmental / timing-related / external, then: ① process was followed ② document what was investigated ③ implement appropriate handling (retry / timeout / explicit error message) ④ add monitoring/logging. **But: 95% of "no root cause" cases are actually insufficient investigation.**
