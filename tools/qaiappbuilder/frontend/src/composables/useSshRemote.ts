// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSshRemote — composable for SSH remote deploy operations.
 *
 * Wraps the /api/remote-deploy/* endpoints:
 *   POST   /api/remote-deploy/connect       → test SSH connectivity
 *   POST   /api/remote-deploy/install       → SSE install-only stream
 *   POST   /api/remote-deploy/start         → SSE start+tunnel stream
 *   POST   /api/remote-deploy/deploy        → SSE install+start stream
 *   GET    /api/remote-deploy/instances     → list instances
 *   DELETE /api/remote-deploy/instances/:id → stop instance
 */
import { ref, readonly } from "vue";
import { apiJson, apiSSE, ApiError, type SseHandler } from "@/api";

/**
 * Remote application port used when the caller does not pick one. Mirrors
 * `DeployRequest.remote_port` in `interfaces/http/routes/remote_deploy.py`.
 *
 * 28688 rather than 8989: the SSH tunnel must bind the SAME port locally that
 * the board listens on (the board derives its Okta `redirect_uri` from its own
 * bound port), and 8989 is already taken by the local QAI instance serving
 * this UI. Both are Okta-registered loopback ports.
 */
export const DEFAULT_REMOTE_PORT = 28688;

/**
 * Remote application ports that can carry an SSO login — the loopback ports
 * registered with Okta (`factory/config/ports.json` `fallbacks`). Anything
 * else fails at Okta with a redirect_uri mismatch, so the backend rejects it
 * when `enable_sso` is set.
 */
export const OKTA_REGISTERED_PORTS = [28688, 8989] as const;

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
  /**
   * Start the remote service behind the Okta login gate (and with a
   * port-scoped session cookie). Requires `remote_port` to be one of the
   * Okta-registered loopback ports.
   */
  enable_sso?: boolean;
}

/**
 * Per-call hooks for {@link useSshRemote}'s `deploy`.
 *
 * The composable's own `deployLog` / `deployPercent` refs are single-valued,
 * which is wrong for a UI that shows several server cards at once — each card
 * needs its own log. `onLine` lets the caller route every SSE progress frame
 * into whatever per-card state it keeps.
 */
export interface DeployHooks {
  onLine?: (line: string, percent: number) => void;
  /** Called with the SSE `event: error` message, which does not throw. */
  onError?: (message: string) => void;
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

  /**
   * URL the browser refused to open in a new tab, or null.
   *
   * A blocked popup is the NORMAL outcome of the auto-open, not an edge case:
   * `start` opens the tab only after its SSE stream finishes, which is tens of
   * seconds past the click that began it. By then the user-activation window has
   * expired and Chrome / Edge drop `window.open` silently — no exception, just a
   * null return. Recording the URL lets the UI offer a real click instead, which
   * is the only thing that can reopen that window.
   */
  const blockedOpenUrl = ref<string | null>(null);

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

  // ── Install / Start / Deploy (SSE) ───────────────────────────────────────

  /**
   * Drive one of the three SSE endpoints, returning the `instance_id` from the
   * `done` frame (null if the stream errored or sent no id).
   *
   * The three endpoints share this wire contract exactly, so they share this
   * plumbing; only the post-stream handling differs.
   */
  async function runStream(
    path: string,
    params: SshDeployParams,
    hooks?: DeployHooks,
  ): Promise<string | null> {
    deploying.value = true;
    deployLog.value = [];
    deployPercent.value = 0;
    deployError.value = null;
    deployedInstance.value = null;
    blockedOpenUrl.value = null;

    const abortCtrl = new AbortController();

    // The instance_id is assigned server-side and delivered in the `done`
    // SSE frame.  Capture it here so we can find the exact instance after
    // loadInstances() refreshes the list.
    let doneInstanceId: string | null = null;

    const handler: SseHandler = {
      onProgress(data: unknown) {
        const d = data as DeployProgress;
        const percent = typeof d.percent === "number" ? d.percent : deployPercent.value;
        if (d.line) {
          // Do NOT auto-open here: this line streams mid-run, before the local
          // tunnel exists, and its URL is the (often unreachable) remote host
          // — opening it races with the correct tunnel-aware open below, which
          // waits for local_url to be ready.
          deployLog.value.push(d.line);
          hooks?.onLine?.(d.line, percent);
        }
        if (typeof d.percent === "number") deployPercent.value = d.percent;
      },
      onDone(data?: unknown) {
        deployPercent.value = 100;
        // Capture the instance_id from the done frame so we can find the
        // exact instance after refreshing the list (avoids picking the wrong
        // instance when multiple runs happened concurrently).
        if (data && typeof data === "object" && "instance_id" in data) {
          doneInstanceId = (data as { instance_id: string }).instance_id;
        }
      },
      onError(err: ApiError) {
        deployError.value = err.message;
        hooks?.onError?.(err.message);
      },
    };

    try {
      await apiSSE(path, handler, {
        method: "POST",
        body: params,
        signal: abortCtrl.signal,
      });
      return doneInstanceId;
    } catch (err) {
      if (!deployError.value) {
        deployError.value = err instanceof ApiError ? err.message : String(err);
      }
      return null;
    } finally {
      deploying.value = false;
    }
  }

  /** Resolve the instance a run targeted, then open it in a new tab. */
  async function resolveAndOpen(
    doneInstanceId: string | null,
    params: SshDeployParams,
  ): Promise<RemoteInstance | null> {
    await loadInstances();
    // Prefer the instance_id from the done SSE frame (precise even when
    // multiple runs happened concurrently); fall back to host+port (this run's
    // target) when the server did not send an id; last resort is the first
    // running instance.
    const targetPort = params.remote_port ?? DEFAULT_REMOTE_PORT;
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
    // Prefer the local tunnel URL — it is the only origin that can complete an
    // SSO login. The direct remote URL is a last resort.
    if (inst?.local_url) {
      openRemoteUrl(inst.local_url);
    } else if (inst?.remote_url) {
      openRemoteUrl(inst.remote_url);
    }
    return inst;
  }

  /** Download the release bundle and run setup.sh. Starts nothing. */
  async function install(
    params: SshDeployParams,
    hooks?: DeployHooks,
  ): Promise<void> {
    await runStream("/api/remote-deploy/install", params, hooks);
    await loadInstances();
  }

  /** Start an installed service, open its tunnel, and open the browser tab. */
  async function start(
    params: SshDeployParams,
    hooks?: DeployHooks,
  ): Promise<RemoteInstance | null> {
    const id = await runStream("/api/remote-deploy/start", params, hooks);
    if (deployError.value) {
      await loadInstances();
      return null;
    }
    return resolveAndOpen(id, params);
  }

  /** Install then start in one call (the original one-click behaviour). */
  async function deploy(
    params: SshDeployParams,
    hooks?: DeployHooks,
  ): Promise<RemoteInstance | null> {
    const id = await runStream("/api/remote-deploy/deploy", params, hooks);
    if (deployError.value) {
      await loadInstances();
      return null;
    }
    return resolveAndOpen(id, params);
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

  /** Open `url` in a new tab. Returns false when the browser blocked it. */
  function openRemoteUrl(url: string): boolean {
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (opened === null) {
      blockedOpenUrl.value = url;
      return false;
    }
    blockedOpenUrl.value = null;
    return true;
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
    install,
    start,
    deploy,
    // instances
    instances: readonly(instances),
    loadingInstances: readonly(loadingInstances),
    loadInstances,
    stopInstance,
    // utils
    openRemoteUrl,
    blockedOpenUrl: readonly(blockedOpenUrl),
  };
}
