// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSshRemote — composable for SSH remote deploy operations.
 *
 * Wraps the /api/remote-deploy/* endpoints:
 *   POST   /api/remote-deploy/connect       → test SSH connectivity
 *   POST   /api/remote-deploy/deploy        → SSE install+start stream
 *   GET    /api/remote-deploy/instances     → list instances
 *   DELETE /api/remote-deploy/instances/:id → stop instance
 */
import { ref, readonly } from "vue";
import { apiJson, apiSSE, ApiError, type SseHandler } from "@/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SshConnectParams {
  host: string;
  ssh_port: number;
  username: string;
  auth_method: "password" | "private_key";
  auth_ref: string;
  key_path?: string;
}

export interface SshDeployParams extends SshConnectParams {
  remote_port?: number;
}

export interface RemoteInstance {
  instance_id: string;
  host: string;
  port: number;
  username: string;
  state: string;
  remote_url: string;
  local_url?: string;
  local_port?: number;
  tunnel_state?: string;
  error_message: string;
}

export interface DeployProgress {
  line: string;
  percent: number;
}

// ---------------------------------------------------------------------------
// Composable
// ---------------------------------------------------------------------------

export function useSshRemote() {
  const connecting = ref(false);
  const connectError = ref<string | null>(null);
  const connectSuccess = ref(false);

  const deploying = ref(false);
  const deployLog = ref<string[]>([]);
  const deployPercent = ref(0);
  const deployError = ref<string | null>(null);
  const deployedInstance = ref<RemoteInstance | null>(null);

  const instances = ref<RemoteInstance[]>([]);
  const loadingInstances = ref(false);

  // ── Connect ──────────────────────────────────────────────────────────────

  async function testConnect(params: SshConnectParams): Promise<boolean> {
    connecting.value = true;
    connectError.value = null;
    connectSuccess.value = false;
    try {
      const res = await apiJson<{ success: boolean; message: string }, SshConnectParams>(
        "POST",
        "/api/remote-deploy/connect",
        params,
      );
      connectSuccess.value = res.success;
      if (!res.success) connectError.value = res.message;
      return res.success;
    } catch (err) {
      connectError.value =
        err instanceof ApiError ? err.message : String(err);
      return false;
    } finally {
      connecting.value = false;
    }
  }

  // ── Deploy (SSE) ─────────────────────────────────────────────────────────

  async function deploy(params: SshDeployParams): Promise<RemoteInstance | null> {
    deploying.value = true;
    deployLog.value = [];
    deployPercent.value = 0;
    deployError.value = null;
    deployedInstance.value = null;

    const abortCtrl = new AbortController();

    // The instance_id is assigned server-side and delivered in the `done`
    // SSE frame.  Capture it here so we can find the exact instance after
    // loadInstances() refreshes the list.
    let doneInstanceId: string | null = null;

    const handler: SseHandler = {
      onProgress(data: unknown) {
        const d = data as DeployProgress;
        if (d.line) {
          // Do NOT auto-open here: this line streams mid-deploy, before the
          // local tunnel exists, and its URL is the (often unreachable)
          // remote host — opening it races with the correct tunnel-aware
          // open in deploy()'s post-SSE handling below, which waits for
          // the local_url to be ready. See onDone / the try block below.
          deployLog.value.push(d.line);
        }
        if (typeof d.percent === "number") deployPercent.value = d.percent;
      },
      onDone(data?: unknown) {
        deployPercent.value = 100;
        // Capture the instance_id from the done frame so we can find the
        // exact instance after refreshing the list (avoids picking the wrong
        // instance when multiple deploys have run concurrently).
        if (data && typeof data === "object" && "instance_id" in data) {
          doneInstanceId = (data as { instance_id: string }).instance_id;
        }
      },
      onError(err: ApiError) {
        deployError.value = err.message;
      },
    };

    try {
      await apiSSE("/api/remote-deploy/deploy", handler, {
        method: "POST",
        body: params,
        signal: abortCtrl.signal,
      });
      // After SSE done, fetch final instance state
      await loadInstances();
      // Find the exact instance: prefer the instance_id from the done SSE
      // frame (precise even when multiple deploys ran concurrently); fall
      // back to host+port (this deploy's target) when the server did not
      // send an id; last resort is the first running instance.
      const targetPort = params.remote_port ?? 8989;
      const inst =
        (doneInstanceId
          ? instances.value.find((i) => i.instance_id === doneInstanceId)
          : undefined) ??
        instances.value.find(
          (i) => i.state === "running" && i.host === params.host && i.port === targetPort,
        ) ??
        instances.value.find((i) => i.state === "running") ??
        null;
      deployedInstance.value = inst;
      // Prefer the local tunnel URL (browser reaches it directly); fall
      // back to the remote URL only when no tunnel was established.
      if (inst?.local_url) {
        openRemoteUrl(inst.local_url);
      } else if (inst?.remote_url) {
        openRemoteUrl(inst.remote_url);
      }
      return inst;
    } catch (err) {
      if (!deployError.value) {
        deployError.value = err instanceof ApiError ? err.message : String(err);
      }
      return null;
    } finally {
      deploying.value = false;
    }
  }

  // ── List instances ────────────────────────────────────────────────────────

  async function loadInstances(): Promise<void> {
    loadingInstances.value = true;
    try {
      const res = await apiJson<{ instances: RemoteInstance[] }>(
        "GET",
        "/api/remote-deploy/instances",
      );
      instances.value = res.instances;
    } catch {
      // non-fatal — keep stale list
    } finally {
      loadingInstances.value = false;
    }
  }

  // ── Stop instance ─────────────────────────────────────────────────────────

  async function stopInstance(instanceId: string): Promise<void> {
    try {
      await apiJson("DELETE", `/api/remote-deploy/instances/${instanceId}`);
      await loadInstances();
    } catch (err) {
      // surface to caller via re-throw
      throw err;
    }
  }

  // ── Open remote URL ───────────────────────────────────────────────────────

  function openRemoteUrl(url: string): void {
    window.open(url, "_blank", "noopener,noreferrer");
  }

  return {
    // connect
    connecting: readonly(connecting),
    connectError: readonly(connectError),
    connectSuccess: readonly(connectSuccess),
    testConnect,
    // deploy
    deploying: readonly(deploying),
    deployLog: readonly(deployLog),
    deployPercent: readonly(deployPercent),
    deployError: readonly(deployError),
    deployedInstance: readonly(deployedInstance),
    deploy,
    // instances
    instances: readonly(instances),
    loadingInstances: readonly(loadingInstances),
    loadInstances,
    stopInstance,
    // utils
    openRemoteUrl,
  };
}
