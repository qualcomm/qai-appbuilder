// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Channels Pinia store.
 *
 * S5 PR-055: wraps /api/{feishu,wechat}/* routes.
 * Exposes: per-kind status, register, start, stop.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";
import type { components } from "@/types/api";

type ChannelInstanceResponse = components["schemas"]["ChannelInstanceResponse"];
type StatusResponse = components["schemas"]["interfaces__http__routes__channels__StatusResponse"];

export type ChannelKind = "feishu" | "wechat";

export interface ChannelState {
  instance: ChannelInstanceResponse | null;
  health: Record<string, unknown>;
}

export const useChannelsStore = defineStore("channels", () => {
  // ─── State ─────────────────────────────────────────────────────────────────
  const channels = ref<Record<ChannelKind, ChannelState>>({
    feishu: { instance: null, health: {} },
    wechat: { instance: null, health: {} },
  });
  const loading = ref(false);
  const error = ref<string | null>(null);

  // ─── Actions ───────────────────────────────────────────────────────────────
  async function fetchStatus(kind: ChannelKind, instanceId: string): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<StatusResponse>("GET", `/api/${kind}/status`, undefined, {
        query: { instance_id: instanceId },
      });
      channels.value[kind] = {
        instance: res.instance,
        health: res.health as Record<string, unknown>,
      };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function startChannel(kind: ChannelKind, instanceId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", `/api/${kind}/start`, { instance_id: instanceId });
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function stopChannel(kind: ChannelKind, instanceId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", `/api/${kind}/stop`, { instance_id: instanceId });
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function registerChannel(
    kind: ChannelKind,
    name: string,
    secretService: string,
    secretKey: string,
    secretValue: string,
  ): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", `/api/${kind}/register`, {
        name,
        secret_service: secretService,
        secret_key: secretKey,
        secret_value: secretValue,
      });
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  return {
    channels,
    loading,
    error,
    fetchStatus,
    startChannel,
    stopChannel,
    registerChannel,
  };
});
