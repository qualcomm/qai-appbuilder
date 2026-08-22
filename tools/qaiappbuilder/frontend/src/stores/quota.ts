// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Token-pool balance store (QAI Service).
 *
 * The QAI Service token pool meters LLM usage per signed-in user, so the user
 * needs to see how much of their allowance is left. The balance reaches us two
 * ways, and this store is the single place both converge:
 *
 * 1. **Seeded once** at app mount from `GET /api/quota/me` — needed because a
 *    freshly opened window has not streamed anything yet, so it has no balance
 *    to show.
 * 2. **Updated for free** by the `quota_usage` stream frame the broker appends
 *    to every answer. That is why there is deliberately NO polling loop here:
 *    the number only changes when the user spends tokens, and the stream
 *    already tells us exactly then.
 *
 * `available` stays false until a balance is actually known, which is what the
 * sidebar widget keys its `v-if` off — a deployment without the token pool
 * (no `base_url` configured, or the user's models are all local / BYO-key)
 * shows nothing at all rather than an empty or zeroed gauge.
 */

import { defineStore } from "pinia";

import { apiJson } from "@/api";

/** Broker balance object, as carried by `GET /api/quota/me` and the frame. */
export interface QuotaBalance {
  allocated: number;
  used: number;
  remaining: number;
  /** Tokens held for in-flight requests; absent on older brokers. */
  reserved?: number;
  /** ISO-8601 instant the allowance resets, or null when it never does. */
  reset_at?: string | null;
  /** e.g. `"monthly"`. */
  period?: string;
  /** e.g. `"tokens"`. */
  unit?: string;
}

interface QuotaState {
  /** Latest known balance, or `null` when none has arrived yet. */
  balance: QuotaBalance | null;
  /** True once a seed attempt finished (success or graceful failure). */
  loaded: boolean;
}

/**
 * Why a `seed()` attempt did or did not produce a balance.
 *
 * The distinction matters for recovery: `"no_session_token"` is the ONE case a
 * silent re-authentication can fix — the broker credential is missing (never
 * minted this session, or the persisted one expired) while the browser's
 * session cookie is still valid, so a forced Okta re-exchange can re-mint it.
 * Every other outcome is either "the pool is irrelevant here"
 * (`"not_configured"`) or a transient fault, neither of which re-authentication
 * would help.
 */
export type QuotaSeedResult =
  | "ok"
  | "no_session_token"
  | "not_configured"
  | "unavailable";

/**
 * Narrow an untrusted object to a usable balance.
 *
 * Only `allocated` and `remaining` are load-bearing — they are what the gauge
 * renders — so a payload missing either is rejected outright rather than
 * displayed as a partial or zeroed bar. Optional fields are passed through
 * as-is: they are the broker's to define, and a field we do not recognise
 * today should survive to the template rather than be dropped here.
 */
function toBalance(raw: unknown): QuotaBalance | null {
  if (raw === null || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const allocated = obj.allocated;
  const remaining = obj.remaining;
  if (typeof allocated !== "number" || !Number.isFinite(allocated)) return null;
  if (typeof remaining !== "number" || !Number.isFinite(remaining)) return null;
  const used = obj.used;
  return {
    allocated,
    remaining,
    used: typeof used === "number" && Number.isFinite(used) ? used : 0,
    ...(typeof obj.reserved === "number" ? { reserved: obj.reserved } : {}),
    ...(typeof obj.reset_at === "string" || obj.reset_at === null
      ? { reset_at: obj.reset_at as string | null }
      : {}),
    ...(typeof obj.period === "string" ? { period: obj.period } : {}),
    ...(typeof obj.unit === "string" ? { unit: obj.unit } : {}),
  };
}

export const useQuotaStore = defineStore("quota", {
  state: (): QuotaState => ({
    balance: null,
    loaded: false,
  }),

  getters: {
    /** Whether a balance is known and therefore worth rendering. */
    available: (state): boolean => state.balance !== null,

    /**
     * Remaining share as a percentage clamped to [0, 100].
     *
     * A zero / negative `allocated` yields 0 rather than a division blow-up:
     * "no allowance" reads as "nothing left", which is the honest rendering.
     */
    remainingPercent: (state): number => {
      const b = state.balance;
      if (b === null || b.allocated <= 0) return 0;
      return Math.max(0, Math.min(100, (b.remaining / b.allocated) * 100));
    },
  },

  actions: {
    /**
     * Seed the balance once at app mount.
     *
     * Best-effort by design: the endpoint returns `{quota: null}` whenever the
     * pool is disabled, unreachable, or the user holds no broker token, and a
     * network failure is swallowed the same way. In every one of those cases
     * the widget simply stays hidden — a balance readout must never surface an
     * error at a user who may not even be using the pool.
     *
     * Returns WHY the attempt ended the way it did (see `QuotaSeedResult`) so
     * the caller can attempt a silent re-authentication for the one recoverable
     * case. The return value is purely advisory: ignoring it preserves the
     * original fire-and-forget behaviour.
     */
    async seed(): Promise<QuotaSeedResult> {
      try {
        const res = await apiJson<{ quota?: unknown; error?: unknown }>(
          "GET",
          "/api/quota/me",
        );
        const parsed = toBalance(res?.quota ?? null);
        if (parsed !== null) {
          this.balance = parsed;
          return "ok";
        }
        // The route always answers 200 and names the reason, so a null balance
        // is classified rather than guessed at.
        const reason = typeof res?.error === "string" ? res.error : "";
        if (reason === "no_session_token") return "no_session_token";
        if (reason === "not_configured") return "not_configured";
        return "unavailable";
      } catch {
        // Leave `balance` as-is (usually null → widget hidden).
        return "unavailable";
      } finally {
        this.loaded = true;
      }
    },

    /**
     * Apply a balance carried by a `quota_usage` stream frame.
     *
     * Rejects an unusable payload instead of clearing a good value: a
     * malformed frame mid-conversation should leave the last known balance on
     * screen, not blank the gauge the user was watching.
     */
    applyFrameQuota(raw: unknown): void {
      const parsed = toBalance(raw);
      if (parsed === null) return;
      this.balance = parsed;
      this.loaded = true;
    },
  },
});
