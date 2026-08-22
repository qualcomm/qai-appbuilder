// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * QGenie dual-bucket quota store.
 *
 * QGenie meters every request into one of two INDEPENDENT allowances and picks
 * which one purely from the outbound `User-Agent`: a UA containing
 * `claude-cli` is billed to its `UI` (IDE/CLI) bucket, anything else to `Api`
 * (API/SDK). The two carry genuinely different limits — measured on one
 * account, `gpt-5.5` grants the IDE/CLI side 5x the daily tokens — so one can
 * run dry while the other is untouched. That is the whole point of this
 * feature: when the bucket in use is exhausted the user can keep working.
 *
 * Deliberately NOT polled, unlike a naive "refresh every N seconds" gauge:
 *
 * 1. **Switching onto a QGenie model** seeds it, because a window that has not
 *    streamed anything has nothing to show.
 * 2. **The end of each turn** refreshes it, which is exactly when the numbers
 *    can have moved.
 *
 * A read costs ~860 ms upstream and QGenie throttles those endpoints at
 * roughly 60 s, so a polling loop would spend the budget the exhaustion check
 * needs while adding nothing — the daily counters only move when the user
 * spends tokens.
 *
 * This is a SEPARATE store from `quota.ts` (the qai-service token pool) on
 * purpose: that balance rides along on every broker answer as a `quota_usage`
 * frame and is a single pool, whereas this one must be pulled and is two
 * buckets x four windows. Folding them together would fit neither.
 */

import { defineStore } from "pinia";

import { apiJson } from "@/api";
import { fetchCloudProviders, putModelTrafficClass } from "@/api/cloudModels";

/**
 * Provider id the internal edition seeds the QGenie catalog under.
 *
 * Mirrors `GetQGenieQuotaUseCase.QGENIE_PROVIDER_ID` and the
 * `[cloud_providers.qgenie]` table it reads.
 */
const QGENIE_PROVIDER_ID = "qgenie";

/** Wire spellings QGenie uses; the UI labels them API/SDK and IDE/CLI. */
export type TrafficClass = "Api" | "UI";

/** One rate-limit window. */
export interface QGenieCounter {
  limit: number;
  used: number;
  remaining: number;
  reset_in_seconds: number;
}

/**
 * The four windows of one traffic class.
 *
 * All optional: QGenie omits some per model (e.g. `claude-4-5-sonnet` reports
 * no weekly counter on the Api side), and a missing counter must read as
 * "unknown" rather than a zeroed bar.
 */
export interface QGenieBucket {
  day?: QGenieCounter | null;
  week?: QGenieCounter | null;
  rpm?: QGenieCounter | null;
  tpm?: QGenieCounter | null;
}

export interface QGenieModelQuota {
  Api: QGenieBucket;
  UI: QGenieBucket;
}

/**
 * Account-wide spend.
 *
 * Has NO model dimension — QGenie's observe-only writer does not store model
 * identifiers, so this is the total across every QGenie model. The tooltip
 * MUST say so, or a reader will take it for the selected model's spend.
 */
export interface QGenieCost {
  currency: string;
  day_usd: number;
  day_by_class?: Record<string, number> | null;
  month_usd: number;
  tier?: string | null;
  day_cap_usd?: number | null;
  month_cap_usd?: number | null;
}

export interface QGenieQuotaPayload {
  available: boolean;
  user: string;
  fetched_at: string;
  stale: boolean;
  /**
   * Why there is nothing (or only stale data) to show.
   *
   * `no_api_key` / `rate_limited` / `unreachable` / `not_configured` /
   * `bad_base_url` / `http_<code>`. Without it every failure looks the same to
   * the user — a vanished gauge reads identically whether they never pasted a
   * key, are being throttled for another 40 seconds, or the gateway is down —
   * and those call for completely different actions.
   */
  error?: string | null;
  models: Record<string, QGenieModelQuota>;
  cost?: QGenieCost | null;
}

/**
 * How far the escalating prompt has gone for one model+class pair.
 *
 * A purely threshold-based check would re-fire on EVERY turn once the line is
 * crossed, so a user who chose to keep going gets interrogated after every
 * reply. Recording how far we have escalated makes each stage fire at most
 * once, while still leaving one last warning before the allowance runs out.
 */
export type PromptStage = "none" | "warned" | "final";

interface QGenieQuotaState {
  payload: QGenieQuotaPayload | null;
  loading: boolean;
  /**
   * Per-model chosen bucket, persisted for the session.
   *
   * Keyed by model because the allowances are per-model: letting one exhausted
   * model force every other onto its fallback would be wrong (and is exactly
   * the bug a single global flag would introduce).
   */
  preferred: Record<string, TrafficClass>;
  /**
   * Reason the most recent read produced nothing usable.
   *
   * Kept OUTSIDE `payload` because the interesting case is precisely when there
   * is no payload: a first-launch throttle or a missing key yields no snapshot
   * at all, and that is exactly when the user needs to be told why.
   */
  lastError: string | null;
  /**
   * Escalation state per `"<modelId>|<class>"`.
   *
   * Keyed by CLASS as well as model: after a switch the new side starts fresh,
   * so its own approach to the line is still announced. Without that, switching
   * would silence the warning entirely and the user would hit a mid-reply
   * cutoff with no notice.
   */
  promptStage: Record<string, PromptStage>;
  /**
   * Model ids the user can actually pick in the main model selector.
   *
   * The upstream reports allowances for every model the ACCOUNT is entitled to
   * (~40), but the catalog this install offers is the provider config's
   * `models[]` — the exact list `useOcModels.availableModels` builds the
   * dropdown from. Showing the other ~35 would offer switches for models the
   * user cannot select, so the table filters to this set.
   *
   * Empty means "not known yet" (hydrate has not run or failed), which must
   * read as "do not filter" rather than "show nothing" — otherwise a failed
   * side-fetch would blank a table whose own data arrived fine.
   */
  selectableModelIds: string[];
}

/** Bucket used before the user has been switched off it. */
export const DEFAULT_TRAFFIC_CLASS: TrafficClass = "UI";

/** Fallback warn threshold (percent of the daily allowance). */
export const DEFAULT_WARN_PERCENT = 95;

/**
 * Share at which the LAST warning fires, regardless of the configured
 * threshold.
 *
 * The first prompt is advisory — the user may well want to spend the remaining
 * few percent, which on a daily allowance is still millions of tokens. This
 * second line exists so that choice does not end in a silent mid-reply cutoff.
 */
export const FINAL_WARN_RATIO = 0.99;

function toCounter(raw: unknown): QGenieCounter | null {
  if (raw === null || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const nums = ["limit", "used", "remaining"].map((k) => o[k]);
  if (nums.some((n) => typeof n !== "number" || !Number.isFinite(n))) return null;
  return {
    limit: o.limit as number,
    used: o.used as number,
    remaining: o.remaining as number,
    reset_in_seconds:
      typeof o.reset_in_seconds === "number" ? o.reset_in_seconds : 0,
  };
}

function toBucket(raw: unknown): QGenieBucket {
  if (raw === null || typeof raw !== "object") return {};
  const o = raw as Record<string, unknown>;
  return {
    day: toCounter(o.day),
    week: toCounter(o.week),
    rpm: toCounter(o.rpm),
    tpm: toCounter(o.tpm),
  };
}

/**
 * Narrow an untrusted response.
 *
 * Rejects a payload with no models: every backend failure path answers exactly
 * that, so treating it as "nothing to show" keeps the gauge hidden without the
 * store having to classify why the read failed.
 */
function toPayload(raw: unknown): QGenieQuotaPayload | null {
  if (raw === null || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  if (o.available !== true) return null;
  const rawModels = o.models;
  if (rawModels === null || typeof rawModels !== "object") return null;
  const models: Record<string, QGenieModelQuota> = {};
  for (const [id, entry] of Object.entries(rawModels as Record<string, unknown>)) {
    const e = (entry ?? {}) as Record<string, unknown>;
    models[id] = { Api: toBucket(e.Api), UI: toBucket(e.UI) };
  }
  if (Object.keys(models).length === 0) return null;
  return {
    available: true,
    user: typeof o.user === "string" ? o.user : "",
    fetched_at: typeof o.fetched_at === "string" ? o.fetched_at : "",
    stale: o.stale === true,
    error: typeof o.error === "string" && o.error !== "" ? o.error : null,
    models,
    cost: (o.cost ?? null) as QGenieCost | null,
  };
}

/** Longest-suffix match, tolerating the several id shapes chat carries. */
function lookup(
  models: Record<string, QGenieModelQuota>,
  modelId: string,
): QGenieModelQuota | null {
  const exact = models[modelId];
  if (exact !== undefined) return exact;
  const bare = modelId.split("::").pop() ?? modelId;
  for (const [key, value] of Object.entries(models)) {
    if ((key.split("::").pop() ?? key) === bare) return value;
  }
  return null;
}

function dayRatio(bucket: QGenieBucket | undefined): number {
  const day = bucket?.day;
  if (!day || day.limit <= 0) return 1;
  return Math.min(1, Math.max(0, day.used / day.limit));
}

/**
 * Daily allowance spent?
 *
 * Daily ONLY, by design: RPM/TPM are rolling windows that recover within a
 * minute, so letting them declare exhaustion would bounce the active bucket
 * back and forth for no user benefit. A missing daily counter reads as "not
 * exhausted" — refusing to send on absent data is worse than letting the
 * upstream decide.
 */
function isExhausted(bucket: QGenieBucket | undefined): boolean {
  const day = bucket?.day;
  if (!day) return false;
  return day.limit <= 0 || day.remaining <= 0;
}

/**
 * Tail of the serialised provider-config write chain.
 *
 * Module-scoped rather than store state because a Promise is not serialisable
 * (and devtools would choke on it); the store instance is a per-app singleton,
 * so a module-level tail has exactly the same lifetime.
 */
let writeChain: Promise<void> = Promise.resolve();

export const useQGenieQuotaStore = defineStore("qgenieQuota", {
  state: (): QGenieQuotaState => ({
    payload: null,
    loading: false,
    preferred: {},
    promptStage: {},
    lastError: null,
    selectableModelIds: [],
  }),

  getters: {
    /** Whether a snapshot is known and therefore worth rendering. */
    available: (state): boolean => state.payload !== null,

    /** True when showing the last good read after a refresh failed. */
    stale: (state): boolean => state.payload?.stale === true,

    /** Account spend across ALL QGenie models (never per-model). */
    cost: (state): QGenieCost | null => state.payload?.cost ?? null,

    /**
     * Reason the gauge has nothing (or only stale data) to show, or `null`.
     *
     * Prefers the live payload's reason over the last recorded one so a
     * recovered read clears it.
     */
    errorReason: (state): string | null =>
      state.payload?.error ?? state.lastError,
  },

  actions: {
    /** Both buckets for `modelId`, or `null` when unknown. */
    quotaFor(modelId: string): QGenieModelQuota | null {
      const models = this.payload?.models;
      if (!models || !modelId) return null;
      return lookup(models, modelId);
    },

    /** Which bucket requests for `modelId` currently go to. */
    classFor(modelId: string): TrafficClass {
      return this.preferred[modelId] ?? DEFAULT_TRAFFIC_CLASS;
    },

    /** Daily-use share in [0,1] for one bucket of one model. */
    ratioFor(modelId: string, traffic: TrafficClass): number {
      const quota = this.quotaFor(modelId);
      if (!quota) return 0;
      return dayRatio(quota[traffic]);
    },

    exhaustedFor(modelId: string, traffic: TrafficClass): boolean {
      const quota = this.quotaFor(modelId);
      if (!quota) return false;
      return isExhausted(quota[traffic]);
    },

    /**
     * Both buckets dry?
     *
     * The guard that stops the switch logic from looping: with both sides
     * spent the only honest move is to tell the user to pick another model.
     */
    bothExhausted(modelId: string): boolean {
      const quota = this.quotaFor(modelId);
      if (!quota) return false;
      return isExhausted(quota.Api) && isExhausted(quota.UI);
    },

    /**
     * Decide whether the user should be prompted, and at which stage.
     *
     * Returns `null` when there is nothing to say. Otherwise a decision the UI
     * renders as ONE dialog listing both sides' headroom, because whether a
     * switch is worth making is the user's call, not ours: "active 96%, other
     * 94%" may still be worth switching for one person and pointless for
     * another, and code guessing that would either nag or stay silent wrongly.
     *
     * Escalation (each stage fires at most once per model+class):
     *   `warned` — crossed the configured threshold. Advisory: the remaining
     *              few percent are still millions of tokens and the user may
     *              well want them.
     *   `final`  — about to run out (`FINAL_WARN_RATIO`). Last chance to switch
     *              before a reply gets cut off mid-sentence.
     *
     * `switchable` is false when the other side is spent too — the dialog then
     * offers only "keep going" and "pick another model", since switching would
     * not help. This is also what stops any possibility of a switch loop.
     */
    quotaDecision(
      modelId: string,
      warnPercent: number,
    ): {
      stage: Exclude<PromptStage, "none">;
      active: TrafficClass;
      other: TrafficClass;
      activeRemaining: number | null;
      otherRemaining: number | null;
      activePercent: number;
      otherPercent: number;
      switchable: boolean;
    } | null {
      const quota = this.quotaFor(modelId);
      if (!quota) return null;

      const active = this.classFor(modelId);
      const other: TrafficClass = active === "Api" ? "UI" : "Api";
      const activeRatio = dayRatio(quota[active]);
      const threshold = Math.min(100, Math.max(1, warnPercent)) / 100;

      // Which stage does the CURRENT usage justify?
      let stage: PromptStage = "none";
      if (activeRatio >= FINAL_WARN_RATIO) stage = "final";
      else if (activeRatio >= threshold) stage = "warned";
      if (stage === "none") return null;

      // Fire only on an ESCALATION: already at "final" stays silent, and a
      // second turn still sitting at "warned" does not re-ask.
      const key = `${modelId}|${active}`;
      const reached = this.promptStage[key] ?? "none";
      const rank: Record<PromptStage, number> = { none: 0, warned: 1, final: 2 };
      if (rank[stage] <= rank[reached]) return null;
      this.promptStage = { ...this.promptStage, [key]: stage };

      const activeDay = quota[active].day;
      const otherDay = quota[other].day;
      return {
        stage,
        active,
        other,
        activeRemaining: activeDay ? activeDay.remaining : null,
        otherRemaining: otherDay ? otherDay.remaining : null,
        activePercent: activeRatio * 100,
        otherPercent: dayRatio(quota[other]) * 100,
        switchable: !isExhausted(quota[other]),
      };
    },

    /**
     * Put `modelId` on a bucket and PERSIST the choice.
     *
     * `target` omitted flips to the other side; passing it explicitly is what
     * lets the dialog offer both classes as direct choices for ANY model
     * (including switching back, which is otherwise unreachable).
     *
     * The write is what actually changes behaviour: the backend picks the
     * outbound `User-Agent` from the model's `params.traffic_class`, so an
     * in-memory-only preference would leave the user's confirmed switch with
     * no effect on the wire while the UI claimed otherwise — worse than not
     * offering the switch at all.
     *
     * Local state is updated first so the table responds immediately; a failed
     * write is rolled back so the highlight can never disagree with what the
     * next request will actually do.
     *
     * Writes are SERIALISED through `writeChain`. Each one is a read-modify-
     * write of the WHOLE provider config document (the backend replaces it
     * wholesale), so two in flight together would both start from the same
     * snapshot and the second would silently drop the first model's change.
     * That is easy to trigger now that every row in the dialog is clickable.
     */
    async switchClass(
      modelId: string,
      target?: TrafficClass,
    ): Promise<TrafficClass> {
      const previous = this.classFor(modelId);
      const next: TrafficClass = target ?? (previous === "Api" ? "UI" : "Api");
      if (next === previous) return previous;
      this.preferred = { ...this.preferred, [modelId]: next };

      const run = async (): Promise<TrafficClass> => {
        try {
          const { providers } = await fetchCloudProviders();
          const config = providers[QGENIE_PROVIDER_ID];
          if (config === undefined) throw new Error("provider not configured");
          await putModelTrafficClass(
            QGENIE_PROVIDER_ID,
            config as unknown as Record<string, unknown>,
            modelId,
            next,
          );
          return next;
        } catch {
          this.preferred = { ...this.preferred, [modelId]: previous };
          return previous;
        }
      };

      // Chain onto whatever write is already running, ignoring its outcome.
      const chained = writeChain.then(run, run);
      // Keep the chain alive even if this link rejects, so one failure cannot
      // wedge every later write.
      writeChain = chained.then(
        () => undefined,
        () => undefined,
      );
      return chained;
    },

    /**
     * Forget the escalation state for one model+class.
     *
     * Called when the daily window has rolled over (`used` fell), so a new day
     * gets its warnings again instead of inheriting yesterday's "already asked".
     */
    resetPromptStage(modelId: string, traffic: TrafficClass): void {
      const key = `${modelId}|${traffic}`;
      if (this.promptStage[key] === undefined) return;
      const next = { ...this.promptStage };
      delete next[key];
      this.promptStage = next;
    },

    /**
     * Load the persisted per-model buckets from the provider config.
     *
     * Needed because the choice lives on the backend (see `switchClass`): a
     * fresh window must show the bucket requests will actually use, not the
     * default. Best-effort — on failure `classFor` falls back to the default,
     * which is also what the backend would apply.
     */
    async hydrate(): Promise<void> {
      try {
        const { providers } = await fetchCloudProviders();
        const config = providers[QGENIE_PROVIDER_ID] as unknown as
          | Record<string, unknown>
          | undefined;
        const models = config?.models;
        if (!Array.isArray(models)) return;
        const next: Record<string, TrafficClass> = {};
        const selectable: string[] = [];
        for (const raw of models) {
          if (raw === null || typeof raw !== "object") continue;
          const entry = raw as Record<string, unknown>;
          const id = entry.model_id;
          if (typeof id !== "string" || id === "") continue;
          // Every configured model is selectable in the main dropdown; the
          // traffic-class preference is optional on top of that, so the id is
          // recorded BEFORE the params check rather than inside it.
          selectable.push(id);
          const params = entry.params;
          if (params === null || typeof params !== "object") continue;
          const value = (params as Record<string, unknown>).traffic_class;
          if (value === "Api" || value === "UI") next[id] = value;
        }
        this.preferred = next;
        this.selectableModelIds = selectable;
      } catch {
        // Keep whatever is already known; the default applies otherwise.
      }
    },

    /**
     * Read the snapshot from the backend.
     *
     * Best-effort: every failure leaves the previous payload in place rather
     * than blanking a gauge the user was watching. `force` bypasses the
     * backend's refresh cooldown and is required before acting on exhaustion,
     * because deciding to switch — or telling the user both sides are dry — on
     * a stale reading could be wrong in either direction.
     */
    async refresh(force = false): Promise<boolean> {
      if (this.loading) return this.payload !== null;
      this.loading = true;
      try {
        const res = await apiJson<unknown>(
          "GET",
          `/api/model-catalog/qgenie-quota${force ? "?force=true" : ""}`,
        );
        const parsed = toPayload(res);
        if (parsed !== null) {
          this.forgetStagesOnRollover(parsed);
          this.payload = parsed;
          this.lastError = parsed.error ?? null;
          return true;
        }
        // Unusable response: keep the previous figures but record WHY the
        // refresh produced nothing, so the gauge can say so instead of just
        // disappearing.
        const raw = res as { error?: unknown } | null;
        this.lastError =
          raw !== null && typeof raw.error === "string" && raw.error !== ""
            ? raw.error
            : "unreachable";
        return false;
      } catch {
        this.lastError = "unreachable";
        return false;
      } finally {
        this.loading = false;
      }
    },

    /**
     * Clear "already asked" for any bucket whose daily counter went DOWN.
     *
     * The upstream resets these at the UTC day boundary. Without this the
     * escalation state would persist across the rollover and a user who
     * dismissed yesterday's warning would get no warning at all today —
     * silently losing the protection they had.
     *
     * Detected by comparing counters rather than by watching a clock: the reset
     * instant belongs to the upstream, and a falling `used` is the observable
     * fact that it happened.
     */
    forgetStagesOnRollover(next: QGenieQuotaPayload): void {
      const previous = this.payload?.models;
      if (!previous) return;
      for (const [modelId, quota] of Object.entries(next.models)) {
        const before = previous[modelId];
        if (before === undefined) continue;
        for (const traffic of ["Api", "UI"] as const) {
          const usedBefore = before[traffic]?.day?.used;
          const usedNow = quota[traffic]?.day?.used;
          if (
            typeof usedBefore === "number" &&
            typeof usedNow === "number" &&
            usedNow < usedBefore
          ) {
            this.resetPromptStage(modelId, traffic);
          }
        }
      }
    },
  },
});
