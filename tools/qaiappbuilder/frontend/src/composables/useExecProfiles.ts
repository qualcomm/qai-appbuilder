// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useExecProfiles` — command-execution profile state (command_policy BC).
 *
 * Owns the read side of the exec-profile broker, the engine the
 * `command_policy_enabled` switch in Security > Tool Safety (layer 3)
 * turns on:
 *
 *   - `GET  /api/security/exec_profiles`        → loaded profiles + enabled
 *   - `POST /api/security/exec_profiles/reload` → re-read `*.toml` from disk
 *
 * Profiles are **static assets** compiled into
 * `factory/config/exec_profiles/*.toml`; there is no write endpoint, so
 * this surface is read-only by design + a reload button (State-Truth-First:
 * the table always shows what the broker actually loaded, never a cached
 * guess).
 *
 * Previously this state lived inside `useSkillCapabilities` and rendered
 * at the bottom of the Skill panel — the wrong semantic domain (profiles
 * gate *commands*, not skills) and the wrong fields (it showed only the
 * legacy `allowed_args` / `denied_args` lists, which every shipped profile
 * leaves EMPTY, so every row rendered as "—"). The guard-rail config that
 * actually classifies a command lives in `ask_args` / `ask_rules` /
 * `hard_deny_args` / `ask_always` / `io_constraints`; those are surfaced
 * here.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

/** One structured subcommand-aware ASK rule (`CommandProfile.ask_rules`). */
export interface ExecAskRule {
  subcommand?: string;
  any_flags?: string[];
  positional_any?: string[];
  reason?: string;
}

/**
 * One entry of `GET /api/security/exec_profiles` `profiles[*]`.
 *
 * Mirrors `interfaces/http/routes/brokers.py::_ExecProfileDTO`. The
 * `allowed_commands` / `deny_patterns` pair are the locked legacy wire
 * names (aliases of `allowed_args` / `denied_args`); new profiles leave
 * them empty and express danger via the guard-rail fields below.
 */
export interface ExecProfile {
  name: string;
  allowed_commands: string[];
  deny_patterns: string[];
  description: string;
  match_glob: string;
  allowed_args: string[];
  denied_args: string[];
  io_constraints: Record<string, unknown>;
  source_skill: string;
  match_globs: string[];
  ask_args: string[];
  hard_deny_args: string[];
  ask_rules: ExecAskRule[];
  ask_always?: boolean;
}

interface ExecProfilesResponse {
  profiles: ExecProfile[];
  enabled: boolean;
}

interface ExecProfilesReloadResponse {
  reloaded: boolean;
  count: number;
}

export function useExecProfiles() {
  const toast = useToastStore();
  const { t } = useI18n();

  const loading = ref(false);
  const reloading = ref(false);
  const profiles = ref<ExecProfile[]>([]);
  /** Broker master state (mirrors `command_policy_enabled`). */
  const enabled = ref(false);
  /** `name` of the profile whose detail rows are expanded (null = none). */
  const expanded = ref<string | null>(null);

  /** Binary match patterns, preferring the multi-glob list. */
  function matchPatterns(profile: ExecProfile): string[] {
    const globs = [...(profile.match_globs ?? [])];
    if (profile.match_glob && !globs.includes(profile.match_glob)) {
      globs.push(profile.match_glob);
    }
    return globs;
  }

  /**
   * Every flag/subcommand that makes an invocation ASK: the flat
   * `ask_args` list plus each `ask_rules` entry rendered as
   * `subcommand flag` (that is how the operator reads it — `git reset
   * --hard`, not two disconnected tokens).
   */
  function askTokens(profile: ExecProfile): string[] {
    const out = [...(profile.ask_args ?? [])];
    for (const rule of profile.ask_rules ?? []) {
      const sub = rule.subcommand ?? "";
      const triggers = [
        ...(rule.any_flags ?? []),
        ...(rule.positional_any ?? []),
      ];
      if (!sub) continue;
      if (!triggers.length) {
        out.push(sub);
        continue;
      }
      for (const trigger of triggers) out.push(`${sub} ${trigger}`);
    }
    return out;
  }

  /** Flags that hard-block (DENY), legacy `denied_args` included. */
  function denyTokens(profile: ExecProfile): string[] {
    const out = [...(profile.hard_deny_args ?? [])];
    for (const arg of profile.denied_args ?? []) {
      if (!out.includes(arg)) out.push(arg);
    }
    return out;
  }

  /** `io_constraints` rendered as `key: v1, v2` lines. */
  function ioConstraintRows(profile: ExecProfile): string[] {
    const io = profile.io_constraints ?? {};
    return Object.entries(io).map(([key, value]) => {
      const rendered = Array.isArray(value)
        ? value.join(", ")
        : String(value ?? "");
      return `${key}: ${rendered}`;
    });
  }

  /** True when the profile carries no classification config at all. */
  function isPermissive(profile: ExecProfile): boolean {
    return (
      !profile.ask_always &&
      !askTokens(profile).length &&
      !denyTokens(profile).length &&
      !ioConstraintRows(profile).length &&
      !(profile.allowed_args ?? []).length
    );
  }

  const profileCount = computed<number>(() => profiles.value.length);

  function toggleExpanded(name: string): void {
    expanded.value = expanded.value === name ? null : name;
  }

  async function fetchProfiles(): Promise<void> {
    loading.value = true;
    try {
      const res = await apiJson<ExecProfilesResponse>(
        "GET",
        "/api/security/exec_profiles",
      );
      profiles.value = Array.isArray(res.profiles) ? res.profiles : [];
      enabled.value = res.enabled === true;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("execBroker.loadFailed", { msg: (e as Error).message }),
        timeoutMs: 4000,
      });
    } finally {
      loading.value = false;
    }
  }

  /** Re-read `factory/config/exec_profiles/*.toml`, then refresh the table. */
  async function reloadProfiles(): Promise<void> {
    reloading.value = true;
    try {
      const res = await apiJson<ExecProfilesReloadResponse>(
        "POST",
        "/api/security/exec_profiles/reload",
      );
      await fetchProfiles();
      toast.push({
        id: crypto.randomUUID(),
        kind: res.reloaded ? "success" : "error",
        message: res.reloaded
          ? t("execBroker.reloaded", { n: res.count })
          : t("execBroker.reloadUnavailable"),
        timeoutMs: 3000,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("execBroker.reloadFailed", { msg: (e as Error).message }),
        timeoutMs: 4000,
      });
    } finally {
      reloading.value = false;
    }
  }

  return {
    loading,
    reloading,
    profiles,
    enabled,
    expanded,
    profileCount,
    matchPatterns,
    askTokens,
    denyTokens,
    ioConstraintRows,
    isPermissive,
    toggleExpanded,
    fetchProfiles,
    reloadProfiles,
  };
}
