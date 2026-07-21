// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePromoteToAppBuilder — Model Builder → App Builder import logic.
 *
 * Encapsulates the full V1 PromoteToAppBuilderCard behaviour (legacy
 * frontend/js/components/model-builder/PromoteToAppBuilderCard.js) so the
 * SFC stays a thin render layer:
 *
 *   (a) Candidate branch — when dry-run surfaces importable plan items,
 *       offer a conflict policy + Validate / Import (commit) / Rollback.
 *   (b) Workspace branch (core) — scan `<workdir>/output/*.bin` for
 *       precision variants, let the user checkbox-select precisions +
 *       pick exactly one default, then one-click Generate (auto-export)
 *       an App Builder Pack.
 *
 * Behaviour, state-machine and number formatting mirror V1; the data
 * source is the redesigned `app_builder` import API (snake_case wire).
 */
import { computed, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  scanBins,
  autoExport,
  importDryRun,
  importCommit,
  importRollback,
  type BinScanResultDTO,
  type NeedsNormalizeDTO,
  type ImportPlanItemDTO,
} from "@/api/appBuilderImport";
import { ApiError } from "@/api";

// ── module-level "user generated a Pack for this workdir" registry ──────────
// Records the workdirs the user has ACTIVELY generated an App Builder Pack for
// during this browser session. Lives at module scope (NOT inside the
// composable) so it survives:
//   * closing + reopening the promote panel (the Card is v-if/v-show toggled),
//   * the composable being re-instantiated when the Card component rebuilds
//     (mode switch / tab switch).
// Keyed by workdir so different models stay isolated. This is what lets
// `showCommitCard` show "generated / ready to import" again after reopening —
// instead of falling back to "re-generate" — WITHOUT reviving the old
// "any on-disk app_pack auto-jumps to Ready" behaviour (a workdir only enters
// this set via a real generatePack() success in THIS session; a stale on-disk
// app_pack from another session is NOT in the set, preserving the guard).
const _generatedWorkdirs = new Set<string>();
// Reactivity trigger: a plain Set is not tracked by `computed`, so bump this
// ref on every add/delete to force dependent computeds to re-evaluate (same
// pattern as usePromoteReadyDetection's dismissBump).
const _generatedBump = ref(0);

function _markGenerated(workdir: string): void {
  if (workdir === "") return;
  _generatedWorkdirs.add(workdir);
  _generatedBump.value++;
}

function _unmarkGenerated(workdir: string): void {
  if (_generatedWorkdirs.delete(workdir)) _generatedBump.value++;
}

function _hasGenerated(workdir: string): boolean {
  // Touch the bump ref so this read is reactive inside a computed.
  void _generatedBump.value;
  return workdir !== "" && _generatedWorkdirs.has(workdir);
}

/** One detected precision variant (workspace branch checklist row). */
export interface VariantRow {
  readonly precision: string;
  readonly label: string;
  readonly sizeBytes: number;
  readonly mtime: string | null;
}

export interface UsePromoteToAppBuilder {
  // ── shared state ──
  readonly loading: Ref<boolean>;
  readonly error: Ref<string>;
  readonly success: Ref<string>;
  readonly hasWorkdir: Ref<boolean>;
  // ── candidate branch ──
  readonly planItems: Ref<ImportPlanItemDTO[]>;
  readonly hasCandidates: Ref<boolean>;
  /** V2 enhancement: when ``true`` the card shows the workspace / pick-precision
   *  stage even though a ready candidate exists, so the user can re-pick a
   *  different precision set and re-generate the Pack. Driven by the
   *  "re-pick precision" link on the commit card; reset on workdir change and
   *  after a successful re-generate. */
  readonly forceVariantPicker: Ref<boolean>;
  /** True when the commit-stage card should render (a candidate exists AND the
   *  user has not asked to re-pick precision). */
  readonly showCommitCard: Ref<boolean>;
  readonly conflictPolicy: Ref<string>;
  readonly importing: Ref<boolean>;
  readonly lastCommitId: Ref<string>;
  readonly warnings: Ref<string[]>;
  /** V1 dry_run parity: hard validation errors (✗) aggregated across the
   *  plan items. Non-empty ⇒ the candidate cannot be imported. */
  readonly errors2: Ref<string[]>;
  /** V1 dry_run parity: conflict notes (⚠) — the target id already exists. */
  readonly conflicts: Ref<string[]>;
  /** V1 dry_run parity: suggested next semver under `bump` (when conflicting). */
  readonly suggestedVersion: Ref<string>;
  /** True after a successful dry-run (Validate) — drives the green pass line.
   *  Reflects REAL importability (a candidate with no blocking errors), NOT
   *  merely "a candidate row exists" (State-Truth-First — no fake "passed"). */
  readonly validated: Ref<boolean>;
  /** True iff every importable plan item has no blocking errors — gates the
   *  Import button so an unimportable (missing weights/runner) candidate can't
   *  be committed. */
  readonly canImport: Ref<boolean>;
  scanCandidates: () => Promise<void>;
  commitImport: () => Promise<void>;
  rollback: () => Promise<void>;
  /** V2 enhancement: switch the card back to the pick-precision stage even when
   *  a ready candidate exists (lets the user re-pick precisions and re-generate
   *  without first importing the stale candidate). */
  showVariantPickerStage: () => void;
  /** Re-scan everything (candidate dry-run + output/ bin variants). Backs the
   *  ↻ refresh button so it actually re-scans `output/` for freshly-built
   *  precisions — V1's button only re-pulled candidates and never re-scanned
   *  bins (a name-doesn't-match-behaviour defect we fix here, not replicate). */
  refresh: () => Promise<void>;
  // ── workspace (multi-variant) branch ──
  readonly variants: Ref<VariantRow[]>;
  /** Set when the scan found NO `output/` variants but detected a downloaded-
   *  but-not-normalized AI Hub model in the workdir. Drives the card's
   *  "run Step 6.5 to normalize" guidance (instead of a blank empty state).
   *  `null` in every normal case (variants found, or genuinely empty). */
  readonly needsNormalize: Ref<NeedsNormalizeDTO | null>;
  readonly checkedPrecisions: Ref<string[]>;
  readonly defaultPrecision: Ref<string>;
  readonly scanLoading: Ref<boolean>;
  readonly showVariantPicker: Ref<boolean>;
  readonly canGenerate: Ref<boolean>;
  readonly exporting: Ref<boolean>;
  togglePrecision: (precision: string) => void;
  setDefaultPrecision: (precision: string) => void;
  generatePack: () => Promise<void>;
  // ── formatting helpers ──
  fmtSize: (bytes: number) => string;
  fmtRelTime: (iso: string | null) => string;
}

export function usePromoteToAppBuilder(
  sessionModelWorkdir: Ref<string>,
  onImported: () => void,
): UsePromoteToAppBuilder {
  const { t } = useI18n();

  // ── shared ──
  const loading = ref(false);
  const error = ref("");
  const success = ref("");
  const hasWorkdir = computed(() => sessionModelWorkdir.value.length > 0);

  // ── candidate branch ──
  const planItems = ref<ImportPlanItemDTO[]>([]);
  const conflictPolicy = ref("bump");
  const importing = ref(false);
  const lastCommitId = ref("");
  const warnings = ref<string[]>([]);
  const errors2 = ref<string[]>([]);
  const conflicts = ref<string[]>([]);
  const suggestedVersion = ref("");
  const hasCandidates = computed(() => planItems.value.length > 0);
  // V2 enhancement: when set, the card shows the workspace / pick-precision
  // stage even though a ready candidate exists, so the user can re-pick a
  // different precision set and re-generate the Pack. Reset on workdir change
  // (the watch below) and after a successful re-generate.
  const forceVariantPicker = ref(false);
  // ── Strict Generate-first ordering (needs-3) ──────────────────────────
  // Tracks whether the user has clicked ``Generate App Builder Pack`` in
  // THIS session and it succeeded. The commit card (Figure 1 — Ready
  // badge + Import button) is gated on this flag: a stale ``app_pack/``
  // directory left over on disk from a PREVIOUS session must NOT auto-
  // surface as "Ready to import" — the user's mental model is
  // "pick precision → Generate → THEN import", not "the workspace
  // magically has a Pack from god-knows-when". Reset on workdir change
  // (the ``watch`` below) so switching to a tab with no session-generated
  // Pack drops the flag even if the target directory has residue.
  //
  // State-Truth-First reading: the "real state" here is not just
  // "does app_pack exist on disk" but "did the current user consciously
  // create it via the current workflow" — a distinction the disk cannot
  // record. We record it in a MODULE-LEVEL registry keyed by workdir (see
  // `_generatedWorkdirs` at module top) so the "generated" fact survives the
  // Card being closed/reopened or the composable being re-instantiated
  // (mode/tab switch) — the previous instance-level `ref` was lost on close,
  // which forced the user back to "re-generate" every reopen.
  // The commit-stage card renders only when:
  //   (1) a candidate exists on disk (backend dry-run resolved one), AND
  //   (2) the user has NOT asked to re-pick precision (existing V2 flag), AND
  //   (3) the user has generated a Pack for THIS workdir in THIS session.
  // Condition (3) enforces the "Generate-first" ordering the user
  // requested — a disk-residue app_pack (from another session, not generated
  // here) no longer auto-jumps past the pick-precision stage.
  const showCommitCard = computed(
    () =>
      hasCandidates.value &&
      !forceVariantPicker.value &&
      _hasGenerated(sessionModelWorkdir.value),
  );
  // V1 parity: tracks whether a successful dry-run (Validate) just produced
  // these planItems AND they are genuinely importable (no blocking errors), so
  // the card renders the green "✓ Validation passed — ready to import" line
  // ONLY when the candidate is really importable. Reset whenever the workdir
  // changes (the existing `watch(sessionModelWorkdir)` re-scans below).
  // State-Truth-First: never show "passed" for a candidate missing
  // weights/runner — that was the fake-success bug.
  const validated = ref(false);
  // Import button gate: every importable item must have no blocking errors.
  const canImport = computed(
    () =>
      hasCandidates.value &&
      errors2.value.length === 0 &&
      planItems.value.some((it) => it.action !== "skip"),
  );

  // ── workspace branch ──
  const variants = ref<VariantRow[]>([]);
  const needsNormalize = ref<NeedsNormalizeDTO | null>(null);
  const checkedPrecisions = ref<string[]>([]);
  const defaultPrecision = ref("");
  const scanLoading = ref(false);
  const exporting = ref(false);
  // Monotonic request token guarding `fetchVariants` against the
  // out-of-order / stale-overwrite race (AGENTS §🔴 State-Truth-First). The
  // "re-pick precision" link, the workdir `watch`, the ↻ refresh and the
  // post-generate re-scan can all dispatch `scanBins` concurrently against a
  // `sessionModelWorkdir` computed that may resolve differently between calls;
  // without a token a late response for an old/empty workdir would clobber the
  // correct one and the card would flip to the "no precision artifacts" state.
  // Only the latest dispatch is allowed to write `variants`.
  let scanToken = 0;

  const showVariantPicker = computed(() => variants.value.length >= 2);
  const canGenerate = computed(() => {
    if (exporting.value) return false;
    if (variants.value.length === 0) return false;
    if (variants.value.length === 1) return true;
    return checkedPrecisions.value.length > 0 && defaultPrecision.value !== "";
  });

  function asMessage(err: unknown): string {
    return err instanceof ApiError ? err.message : String(err);
  }

  // ── candidate branch actions ─────────────────────────────────────────────
  async function scanCandidates(): Promise<void> {
    if (!hasWorkdir.value) return;
    loading.value = true;
    error.value = "";
    // Stale-while-revalidate: do NOT pre-clear the dry-run result state
    // (``errors2`` / ``conflicts`` / ``warnings`` / ``validated`` /
    // ``suggestedVersion``) at the start of a scan. Clearing them
    // synchronously unmounts the ``.promote-card__dryrun`` block (its
    // ``v-if`` OR-guard flips false) which collapses the card's height,
    // and remounts it after the async response, producing a visible
    // "先变窄再恢复" jitter every time the user clicks Validate. Keeping
    // the previous result visible until the fresh values are ready keeps
    // the card height stable; we swap all fields together after the
    // response arrives so the on-screen state stays coherent.
    try {
      const res = await importDryRun([sessionModelWorkdir.value]);
      planItems.value = res.items;
      // Surface provenance / validation reasons as warnings (V1 parity:
      // the dry-run report's warnings drove the ⚠ section).
      warnings.value = res.items
        .filter((it) => it.action === "skip" && it.reason != null)
        .map((it) => String(it.reason));
      // V1 dry_run parity: aggregate hard errors (✗) + conflict notes (⚠) +
      // the suggested next version across all items so the card can render
      // them (replaces the old "validated = has candidates" fake-success).
      errors2.value = res.items.flatMap((it) =>
        Array.isArray(it.errors) ? it.errors.map(String) : [],
      );
      conflicts.value = res.items.flatMap((it) =>
        Array.isArray(it.conflicts) ? it.conflicts.map(String) : [],
      );
      const sv = res.items.find(
        (it) => it.suggested_version != null && it.suggested_version !== "",
      )?.suggested_version;
      suggestedVersion.value = sv != null ? String(sv) : "";
      // State-Truth-First: "validation passed" requires a candidate that is
      // REALLY importable (resolved at least one item AND no blocking errors),
      // not merely "a candidate row exists".
      validated.value =
        planItems.value.length > 0 && errors2.value.length === 0;
    } catch (err) {
      error.value = asMessage(err);
      // On failure we DO clear the stale result so the user isn't left
      // looking at an outdated "passed" line for a request that didn't
      // complete. The card still has an ``error`` line rendered so it does
      // not collapse to a bare header.
      warnings.value = [];
      errors2.value = [];
      conflicts.value = [];
      suggestedVersion.value = "";
      validated.value = false;
    } finally {
      loading.value = false;
    }
  }

  // V2 enhancement: switch the card back to the pick-precision stage even when
  // a ready candidate exists. Re-scan the output/ bins first so the picker
  // reflects what is on disk now, then flip the flag the template watches.
  function showVariantPickerStage(): void {
    forceVariantPicker.value = true;
    // Strict Generate-first ordering (needs-3): re-picking precision means
    // the currently-generated Pack is being abandoned; drop the session flag
    // so ``showCommitCard`` won't auto-jump back to Figure 1 the moment the
    // user finishes ticking checkboxes. The workdir is re-marked only when
    // the new Generate succeeds.
    _unmarkGenerated(sessionModelWorkdir.value);
    void fetchVariants();
  }

  async function commitImport(): Promise<void> {
    if (!hasCandidates.value) return;
    // State-Truth-First: never commit a candidate with blocking errors
    // (missing weights / runner). The button is also disabled in the UI.
    if (!canImport.value) return;
    importing.value = true;
    error.value = "";
    success.value = "";
    try {
      // V1 parity: send the user-chosen conflict policy so the importer can
      // bump the version / replace-with-backup / abort.
      const res = await importCommit(planItems.value, conflictPolicy.value);
      lastCommitId.value = res.commit_id;
      success.value = t("modelBuilder.promote.importSuccess");
      onImported();
    } catch (err) {
      error.value =
        t("modelBuilder.promote.importFailed") + ": " + asMessage(err);
    } finally {
      importing.value = false;
    }
  }

  async function rollback(): Promise<void> {
    if (lastCommitId.value === "") return;
    importing.value = true;
    error.value = "";
    try {
      await importRollback(lastCommitId.value);
      success.value = t("modelBuilder.promote.rollbackSuccess");
      lastCommitId.value = "";
    } catch (err) {
      error.value = asMessage(err);
    } finally {
      importing.value = false;
    }
  }

  // ── workspace branch actions ─────────────────────────────────────────────
  function resetVariants(): void {
    variants.value = [];
    needsNormalize.value = null;
    checkedPrecisions.value = [];
    defaultPrecision.value = "";
  }

  async function fetchVariants(): Promise<void> {
    // Snapshot the workdir at dispatch time so a late response is matched
    // against the workspace it was actually scanned for, not whatever the
    // `sessionModelWorkdir` computed resolves to when the response lands.
    const workdir = sessionModelWorkdir.value;
    if (workdir.length === 0) {
      resetVariants();
      return;
    }
    const token = ++scanToken;
    scanLoading.value = true;
    try {
      const res = await scanBins(workdir);
      // Drop this response if a newer scan has been dispatched meanwhile or the
      // workspace changed under us — prevents an out-of-order / stale-workdir
      // result from overwriting the correct one (the "sometimes normal,
      // sometimes empty" race).
      if (token !== scanToken || sessionModelWorkdir.value !== workdir) return;
      // Only rows the backend decoded into a precision variant are
      // checklist candidates (workspace mode). Legacy listing rows
      // (no precision) are ignored here.
      const decoded: VariantRow[] = res.results
        .filter((r: BinScanResultDTO) => r.precision != null && r.label != null)
        .map((r: BinScanResultDTO) => ({
          precision: String(r.precision),
          label: String(r.label),
          sizeBytes: r.size_bytes,
          mtime: r.mtime ?? null,
        }));
      // Deduplicate by precision (defensive — the backend already dedupes,
      // but the picker keys selection/default by the precision STRING, so a
      // duplicate precision row would share one checkbox state and toggle in
      // lockstep). Keep the first occurrence per precision (backend order is
      // already .bin-preferred / newest-first within a precision).
      const seenPrec = new Set<string>();
      const rows: VariantRow[] = [];
      for (const r of decoded) {
        if (seenPrec.has(r.precision)) continue;
        seenPrec.add(r.precision);
        rows.push(r);
      }
      variants.value = rows;
      // Surface the "un-normalized AI Hub model detected" hint ONLY when no
      // variants were found (backend sets it only in that case). When variants
      // exist it is null. Lets the card guide "run Step 6.5" instead of a
      // blank empty state.
      needsNormalize.value = rows.length === 0 ? (res.needs_normalize ?? null) : null;
      // Default selection: all checked, first as default (V1 parity).
      checkedPrecisions.value = rows.map((r) => r.precision);
      defaultPrecision.value = rows[0]?.precision ?? "";
    } catch {
      // State-Truth-First: a transient scan failure (e.g. `output/` being
      // rewritten mid-export, or a momentary OSError) is NOT proof that the
      // disk has no precision artifacts. Do not clobber a previously-good
      // result into the "no precision artifacts" state — leave `variants` as
      // they were so the card keeps showing the real on-disk precisions. Only
      // the latest dispatch may act here.
      if (token !== scanToken) return;
    } finally {
      // Clear the in-flight flag only for the latest dispatch so a superseded
      // response doesn't prematurely hide the loading state of a newer scan.
      if (token === scanToken) scanLoading.value = false;
    }
  }

  function togglePrecision(precision: string): void {
    const idx = checkedPrecisions.value.indexOf(precision);
    if (idx >= 0) {
      checkedPrecisions.value = checkedPrecisions.value.filter(
        (p) => p !== precision,
      );
      if (defaultPrecision.value === precision) {
        defaultPrecision.value = checkedPrecisions.value[0] ?? "";
      }
    } else {
      checkedPrecisions.value = [...checkedPrecisions.value, precision];
      if (defaultPrecision.value === "") defaultPrecision.value = precision;
    }
  }

  function setDefaultPrecision(precision: string): void {
    // Selecting a default implicitly checks it.
    if (!checkedPrecisions.value.includes(precision)) {
      checkedPrecisions.value = [...checkedPrecisions.value, precision];
    }
    defaultPrecision.value = precision;
  }

  async function generatePack(): Promise<void> {
    if (!hasWorkdir.value || !canGenerate.value) return;
    exporting.value = true;
    error.value = "";
    success.value = "";
    try {
      const precisions =
        variants.value.length >= 1 && checkedPrecisions.value.length > 0
          ? [...checkedPrecisions.value]
          : undefined;
      const res = await autoExport({
        source_path: sessionModelWorkdir.value,
        ...(precisions !== undefined
          ? {
              precisions,
              default_precision:
                defaultPrecision.value !== ""
                  ? defaultPrecision.value
                  : precisions[0],
            }
          : {}),
      });
      if (res.success) {
        success.value = t("modelBuilder.promote.packGenerated", {
          name: res.display_name !== "" ? res.display_name : res.pack_id,
        });
        // V1 parity (PromoteToAppBuilderCard.js:226-228): generating a Pack is
        // only step ① (write `<workdir>/app_pack/` to disk). It does NOT import
        // the model into the DB and MUST NOT fire `onImported` — that callback
        // closes the panel + reloads App Builder, which is only correct after
        // the real commit (step ②). So here we just drop any forced
        // pick-precision override and re-scan, surfacing the freshly-generated
        // candidate IN-PLACE so the commit card appears and the user can click
        // "Import to App Builder" to finish step ②. (V2 regressed by calling
        // onImported here, closing the panel before the commit card showed —
        // leaving the model generated-but-never-imported.)
        forceVariantPicker.value = false;
        // Strict Generate-first ordering (needs-3): flip the session flag so
        // ``showCommitCard`` can advance past the pick-precision stage. This
        // is the ONLY code path that sets the flag — the flag is not persisted
        // across workdir switches (see the ``watch`` below) or full reloads,
        // so a stale ``app_pack/`` on disk can never trick the card into
        // Figure-1 (Ready) without the user consciously clicking Generate in
        // the current session. Recorded in the module-level registry so it
        // survives closing/reopening the panel and composable rebuilds.
        _markGenerated(sessionModelWorkdir.value);
        await scanCandidates();
      } else {
        error.value =
          res.errors.length > 0 ? res.errors.join("; ") : res.note;
      }
    } catch (err) {
      error.value = asMessage(err);
    } finally {
      exporting.value = false;
    }
  }

  // ── formatting helpers (V1 parity) ───────────────────────────────────────
  function fmtSize(bytes: number): string {
    if (!bytes || bytes <= 0) return "–";
    const mb = bytes / (1024 * 1024);
    if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
  }

  function fmtRelTime(iso: string | null): string {
    if (iso == null || iso === "") return "";
    const ts = Date.parse(iso);
    if (Number.isNaN(ts)) return iso;
    const diffSec = Math.max(0, (Date.now() - ts) / 1000);
    if (diffSec < 60) return t("modelBuilder.promote.relativeTime.justNow");
    const mins = Math.floor(diffSec / 60);
    if (mins < 60)
      return t("modelBuilder.promote.relativeTime.minutesAgo", { n: mins });
    const hrs = Math.floor(mins / 60);
    return t("modelBuilder.promote.relativeTime.hoursAgo", { n: hrs });
  }

  // Refetch both branches when the session model workspace changes.
  watch(
    sessionModelWorkdir,
    () => {
      // A different workspace means any forced pick-precision override no
      // longer applies — reset it so the new workspace shows its natural stage.
      forceVariantPicker.value = false;
      // Strict Generate-first ordering (needs-3): switching workspaces drops
      // the session-generated flag so the new workspace shows its
      // pick-precision stage first even if it has a stale ``app_pack/`` on
      // disk. The "generated" fact now lives in a module-level registry keyed
      // by workdir (`_generatedWorkdirs`), so switching workdir needs NO reset
      // here — a different workdir simply isn't in the set (unless it too was
      // generated this session, which is exactly the state we want to keep).
      void scanCandidates();
      void fetchVariants();
    },
    { immediate: true },
  );

  // ↻ refresh button: re-scan BOTH the candidate dry-run AND the output/ bin
  // variants, so a precision freshly built in Model Builder shows up without
  // reopening the card (fixes V1's refresh-doesn't-rescan-bins defect).
  async function refresh(): Promise<void> {
    await Promise.all([scanCandidates(), fetchVariants()]);
  }

  return {
    loading,
    error,
    success,
    hasWorkdir,
    planItems,
    hasCandidates,
    forceVariantPicker,
    showCommitCard,
    conflictPolicy,
    importing,
    lastCommitId,
    warnings,
    errors2,
    conflicts,
    suggestedVersion,
    validated,
    canImport,
    scanCandidates,
    commitImport,
    rollback,
    showVariantPickerStage,
    refresh,
    variants,
    needsNormalize,
    checkedPrecisions,
    defaultPrecision,
    scanLoading,
    showVariantPicker,
    canGenerate,
    exporting,
    togglePrecision,
    setDefaultPrecision,
    generatePack,
    fmtSize,
    fmtRelTime,
  };
}
