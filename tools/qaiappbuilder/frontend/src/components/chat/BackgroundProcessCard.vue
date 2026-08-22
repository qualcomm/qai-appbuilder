<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * BackgroundProcessCard — in-conversation rich-interaction card for the
 * platform ``background_process`` LLM tool (V2 enhancement; no V1 equivalent).
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList →
 * ToolCallList) in place of the generic ``ToolExecPanel`` whenever
 * ``call.tool === "background_process"`` — mirroring how ``todowrite`` renders
 * ``TaskListCard`` and ``question`` renders ``ChatQuestionCard``.
 *
 * Display modes (driven by ``args.action`` — the tool's first positional arg):
 *   - "list"                              → list of every tracked process
 *   - "start" / "status" / "stop" / "restart"
 *                                         → single-process detail card with
 *                                           Logs / Stop / Restart buttons
 *   - "logs"                              → captured output viewer
 *   - any ``ok === false``                → red error card
 *
 * --- State-truth handling (AGENTS.md §🔴 State-Truth-First, rule 1) ----------
 *
 * The card does NOT parse the ``result`` string the model received.  By the
 * time a ``tool_result`` frame reaches this component its ``result`` field has
 * been ``str(...)``-coerced + truncated for LLM consumption (see
 * ``streaming.py`` line 7245-7246), so it is a Python dict repr / a tail of
 * the captured log buffer — neither is structured JSON we can faithfully
 * reparse on the client.
 *
 * Instead we use the call's ``arguments`` (the action + id are reliable —
 * they are the JSON the model emitted, preserved verbatim in ``args``) and on
 * mount we hit the authoritative HTTP endpoints
 * (``GET /api/background_process[/{id}[/logs]]``) for the real state, then
 * subscribe to the global ``/api/events`` SSE stream to receive
 * ``background_process.updated`` envelopes in real time.  This is the
 * "real-resource probe over inferred state" pattern AGENTS.md mandates: the
 * card always shows the manager's actual state, not a stale ``result`` text.
 *
 * For ``action === "start"`` the args do NOT carry the id (the manager
 * assigns it).  We salvage it from the result string with a forgiving regex
 * (``bgp_[A-Za-z0-9_-]+``) — works whether the result is the dict repr or a
 * future rendered string projection — and fall back to a "(starting…)"
 * placeholder while we wait for the matching ``background_process.updated``
 * event to surface the real Info.
 *
 * Buttons (Logs / Stop / Restart) hit the same HTTP routes; the manager
 * publishes ``BackgroundProcessUpdated`` so the subscribed SSE refreshes the
 * displayed info without a follow-up poll.  Logs render in a collapsible
 * panel inside the card (theme tokens, monospaced) instead of a centred
 * modal so the card scrolls with the conversation (same affordance as
 * TaskListCard / ChatQuestionCard).
 *
 * --- Collapse contract (shared with ``ToolExecPanel``) ----------------------
 *
 * The card reuses the exec panel's three-state fold mechanism verbatim:
 * ``collapsed`` (the user/global preference) + ``userToggled`` (freezes the
 * automatic transitions once the user has expressed a choice) +
 * ``effectiveCollapsed`` (the display-only mask), driven by header clicks and
 * by the topbar bulk-collapse broadcast (``ui.toolCardsCollapsed`` +
 * ``ui.toolCardsBroadcastTick``).
 *
 * ONE parameter differs deliberately, and it inverts the exec default:
 * ``ToolExecPanel`` forces "running ⇒ expanded, terminal ⇒ auto-collapse",
 * which fits a command that stops being interesting the moment it finishes.
 * A background process is the opposite shape — a dev server / watcher stays
 * ``running`` for the whole session, so that rule would pin the card open
 * FOREVER and no click could fold it. This card therefore:
 *
 *   - starts COLLAPSED (a long-lived process's normal state is "I know it is
 *     up, I do not need to stare at it"),
 *   - honours a user toggle in either direction (``userToggled`` semantics
 *     unchanged),
 *   - force-expands only on an ABNORMAL termination (``failed`` / non-zero
 *     exit code) — the one moment the details are what the user needs,
 *   - and still follows the topbar bulk-collapse broadcast.
 *
 * Same mechanism, different lifecycle parameters — not a second convention.
 *
 * Theme tokens only (``--success`` / ``--accent`` / ``--warning`` / ``--error`` /
 * ``--text-*`` / ``--bg-*`` / ``--border``), no hard-coded colours — keeps the
 * card aligned with the rest of chat under light/dark theme switches.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import {
  fetchBgpInfo,
  fetchBgpList,
  fetchBgpLogs,
  restartBgp,
  stopBgp,
  type BgpInfo,
  type BgpStatus,
} from "@/api/backgroundProcess";
import {
  connectGlobalEvents,
  type GlobalSseEvent,
} from "@/api/globalEvents";
import { useUiStore } from "@/stores/ui";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/**
 * The card receives the wire-side ``arguments`` of the ``background_process``
 * call (action + spawn spec) plus the tool's serialised ``result`` text.
 *
 * ``result`` is intentionally typed as ``unknown`` because the back-end has
 * already projected the structured handler return through
 * ``str(...) -> truncator``: it may be a Python dict repr, a head+tail
 * summary, or — in some legacy code paths — the raw object (when reloaded
 * straight off a persisted ChatToolCall.output without round-trip).  We
 * only use it to ``ok === false`` -path detection (string sentinel
 * ``[tool_error]`` / ``[guardrail_blocked]``) and to salvage an id for the
 * "start" action.  All other state comes from the HTTP + SSE truth source.
 */
const props = defineProps<{
  /** Raw ``arguments`` of the background_process tool call. */
  args: Record<string, unknown>;
  /** The tool result text (may be empty while running; may be a Python dict
   *  repr post-truncation). */
  result?: unknown;
  /** Always one of the tool's six actions; "unknown" when the upstream
   *  could not infer it (defensive — the card still renders sensibly). */
  action: string;
}>();

const { t } = useI18n();

// ---------------------------------------------------------------------------
// Action derivation + id salvage
// ---------------------------------------------------------------------------

/** Allowed action values (mirrors the back-end schema enum). */
type Action = "list" | "start" | "status" | "stop" | "restart" | "logs" | "wait";
const VALID_ACTIONS = new Set<Action>([
  "list",
  "start",
  "status",
  "stop",
  "restart",
  "logs",
  "wait",
]);

/** Resolved action — defaults to ``"status"`` when the upstream lost it; for
 *  any unknown value the card still picks a sensible single-process layout. */
const resolvedAction = computed<Action>(() => {
  const a = props.action;
  if (VALID_ACTIONS.has(a as Action)) return a as Action;
  // Fallback: args.action may still be present (legacy frames lost ``action``
  // prop wiring).
  const argA = props.args["action"];
  if (typeof argA === "string" && VALID_ACTIONS.has(argA as Action)) {
    return argA as Action;
  }
  return "status";
});

/** Result, coerced to a printable string for ``ok === false`` detection and
 *  id salvage. */
const resultText = computed<string>(() => {
  const r = props.result;
  if (r === null || r === undefined) return "";
  if (typeof r === "string") return r;
  try {
    return JSON.stringify(r);
  } catch {
    return String(r);
  }
});

/** Detect the back-end's synthetic error sentinels — they DO ride this card
 *  whenever the model called the tool with disallowed arguments and got a
 *  ``[tool_error] ...`` text back. */
const isErrorResult = computed<boolean>(() => {
  const s = resultText.value;
  return s.startsWith("[tool_error]") || s.startsWith("[guardrail_blocked]");
});

/** Process id resolved from args (``status`` / ``stop`` / ``restart`` /
 *  ``logs``) or salvaged from the result text (``start``).  ``null`` when
 *  unresolvable — for ``list`` this is fine (card uses the list path);
 *  for single-process modes the card renders an "id pending" placeholder
 *  until the SSE event arrives. */
const BGP_ID_PATTERN = /bgp[_-][A-Za-z0-9_-]{4,}/;
const resolvedId = computed<string | null>(() => {
  const argsId = props.args["id"];
  if (typeof argsId === "string" && argsId.startsWith("bgp")) return argsId;
  const m = resultText.value.match(BGP_ID_PATTERN);
  return m ? m[0] : null;
});

/** Snapshot recovered from the tool result itself.
 *
 *  State-truth rationale (AGENTS.md §5 rule 1): for ``wait`` the tool return
 *  value IS the most direct observation of the process — the handler read the
 *  manager's terminal state and rendered it. A ``wait`` card must therefore
 *  never depend solely on a follow-up ``GET /api/background_process/{id}``:
 *  once the process settles the manager may already have reaped it, so that
 *  probe 404s and the card would fall back to the args-derived placeholder —
 *  which for ``wait`` carries NO command (args are only action/id/timeout_s)
 *  and a hardcoded "starting" status, i.e. it would state the opposite of what
 *  actually happened.
 *
 *  ``render_tool_result_text_with_hints`` renders one process as
 *  ``  <id>  [<status>]  pid=<n> — <description>`` followed by an indented
 *  ``$ <command>`` line; both parts are optional-ish, so every field is
 *  matched independently and a miss simply leaves that field unset. */
const RESULT_STATUS_PATTERN = /\[(starting|running|ready|exited|failed|stopped)\]/;
const RESULT_PID_PATTERN = /\bpid=(\d+)/;
const RESULT_COMMAND_PATTERN = /^\s*\$ (.+)$/m;
const resultSnapshot = computed<Partial<BgpInfo> | null>(() => {
  const s = resultText.value;
  if (s === "" || isErrorResult.value) return null;
  const status = s.match(RESULT_STATUS_PATTERN)?.[1];
  if (status === undefined) return null;
  const pidRaw = s.match(RESULT_PID_PATTERN)?.[1];
  const command = s.match(RESULT_COMMAND_PATTERN)?.[1];
  return {
    status: status as BgpStatus,
    ...(pidRaw !== undefined ? { pid: Number(pidRaw) } : {}),
    ...(command !== undefined ? { command: command.trim() } : {}),
  };
});

// ---------------------------------------------------------------------------
// Reactive state
// ---------------------------------------------------------------------------

/** Single-process Info (single-process modes).  ``null`` = unresolved /
 *  retired (we render a "process already retired" hint then). */
const info = ref<BgpInfo | null>(null);
/** True once we observed a ``background_process.deleted`` event for this
 *  card's id — suppresses the args-derived placeholder so the card shows
 *  the "retired" hint instead of a phantom "starting…" detail panel. */
const retired = ref<boolean>(false);
/** List of processes (``list`` action). */
const processList = ref<ReadonlyArray<BgpInfo>>([]);
/** Captured output for ``logs`` action / Logs button. */
const logsText = ref<string>("");
/** Whether the in-card logs viewer is open (Logs button toggles it). */
const logsOpen = ref<boolean>(false);
/** General "loading" indicator (mount-time fetch + button work). */
const loading = ref<boolean>(false);
/** "SSE not available" sentinel — we fall back to "manual Refresh button"
 *  when ``connectGlobalEvents`` fails (the project's existing helper auto-
 *  retries, so this is almost always ``false``).  Surfaced as a hint line. */
const sseLive = ref<boolean>(true);
/** Async-error message surfaced under the action bar; cleared on each
 *  successful request. */
const asyncError = ref<string>("");

// ---------------------------------------------------------------------------
// Authoritative-state probe
// ---------------------------------------------------------------------------

/** Is the tool result we already hold a TERMINAL observation?
 *
 *  The manager keeps processes in a single in-memory map and drops a settled
 *  one after its retention window, so ``GET /api/background_process/{id}``
 *  for a long-finished process is a request that CANNOT succeed. Every
 *  history card used to fire one on mount anyway (a page reload re-mounts
 *  them all), producing a console full of 404s that hides real failures —
 *  while the authoritative answer was already in hand: ``resultSnapshot`` is
 *  the terminal state the BACK END observed while the process still existed.
 *
 *  ``retired`` counts too: a ``deleted`` event is the manager telling us the
 *  record is gone, so re-probing it is provably pointless. */
const hasTerminalSnapshot = computed<boolean>(
  () => retired.value || isTerminal(resultSnapshot.value?.status),
);

/** Probe the authoritative state.
 *
 *  ``force`` marks an EXPLICIT user request (the Refresh button): the user
 *  asked for a round trip, so we make it even on a terminal card — within the
 *  manager's retention window it answers with the real record, and outside it
 *  the existing snapshot fallback below keeps the card correct. Automatic
 *  callers (mount / id-resolution) leave it ``false`` and skip the request
 *  whenever the outcome is already known. */
async function refreshState(force = false): Promise<void> {
  if (isErrorResult.value || resolvedAction.value === "logs") {
    // logs-only mode: pull the captured output below; nothing else to do.
    if (resolvedAction.value === "logs" && resolvedId.value !== null) {
      loading.value = true;
      try {
        const logs = await fetchBgpLogs(resolvedId.value);
        logsText.value = logs?.output ?? "";
        logsOpen.value = true;
        asyncError.value = "";
      } catch (err) {
        asyncError.value = String((err as Error).message ?? err);
      } finally {
        loading.value = false;
      }
    }
    return;
  }
  if (resolvedAction.value === "list") {
    loading.value = true;
    try {
      const list = await fetchBgpList();
      processList.value = list;
      asyncError.value = "";
    } catch (err) {
      asyncError.value = String((err as Error).message ?? err);
    } finally {
      loading.value = false;
    }
    return;
  }
  // single-process modes (start / status / stop / restart / wait)
  if (resolvedId.value === null) {
    // Nothing to fetch yet; wait for the SSE event to surface the id.
    return;
  }
  if (!force && hasTerminalSnapshot.value) {
    // Terminal short-circuit: the outcome is already known and the manager no
    // longer keeps the record, so the probe could only 404. Render from the
    // snapshot (see ``placeholderInfo``) and stay silent on the wire.
    return;
  }
  loading.value = true;
  try {
    const fresh = await fetchBgpInfo(resolvedId.value);
    if (fresh === null && resultSnapshot.value === null) {
      // ``fetchBgpInfo`` maps a 404 to ``null``: the manager has no record for
      // this id, i.e. the process is gone. Say so. Without this the card fell
      // back to the args-derived placeholder and reported a hardcoded
      // "starting" — claiming a reaped process is still coming up, which is
      // the opposite of the truth. ``retired`` was previously reachable only
      // through a live ``deleted`` event, so after a page reload (SSE long
      // past) it could never be shown at all.
      info.value = null;
      retired.value = true;
    } else {
      // A snapshot outranks a 404: it is an observation the back end made
      // while the process still existed (see ``placeholderInfo``).
      info.value = fresh;
    }
    asyncError.value = "";
  } catch (err) {
    // Any non-404 transport failure. The tool result we already hold is then
    // the better truth source: keep rendering it instead of turning a
    // successful wait into a red error row.
    if (resultSnapshot.value !== null) {
      asyncError.value = "";
    } else {
      asyncError.value = String((err as Error).message ?? err);
    }
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------------
// SSE subscription
// ---------------------------------------------------------------------------

/** Disposer for the global EventSource subscription (closure-bound below). */
let disposeSse: (() => void) | null = null;

/** Apply one ``background_process.updated`` envelope to local state.
 *  Filter discipline:
 *   - single-process modes: only the matching ``info.id``;
 *   - ``list`` mode: any event triggers a list refresh (cheap — the route
 *     returns the in-memory map snapshot, no DB hit).
 *   - ``logs`` mode: only the matching id refreshes the captured tail. */
function applyUpdatedEvent(evt: GlobalSseEvent): void {
  const action = resolvedAction.value;
  // Envelope shape mirrors BackgroundProcessUpdated dataclass:
  // { type: "background_process.updated", info: {...}, scope: "global" }
  const incoming = evt["info"] as BgpInfo | undefined;
  if (incoming === undefined || typeof incoming.id !== "string") return;

  if (action === "list") {
    // Splice or append: the list is small (<= a few dozen per session).
    const next = processList.value.slice();
    const idx = next.findIndex((p) => p.id === incoming.id);
    if (idx >= 0) {
      next[idx] = incoming;
    } else {
      next.push(incoming);
    }
    processList.value = next;
    return;
  }

  // single-process modes: bind once if id is still unknown (the "start"
  // action case where we only had the salvaged id pattern).
  if (resolvedId.value === null || incoming.id === resolvedId.value) {
    info.value = incoming;
    if (logsOpen.value) {
      // The captured output is part of Info — surface it through the logs
      // panel without an extra HTTP round trip.
      logsText.value = incoming.output;
    }
  }
}

/** Apply one ``background_process.deleted`` envelope — the process was
 *  removed from the manager's task map (terminal state + GC).
 *
 *  A ``wait`` card is exempt from the "retired" transition: its whole purpose
 *  is to report the FINAL state, and GC of a settled process is the expected
 *  epilogue rather than new information. Retiring it would replace a correct
 *  ``[exited] pid=…`` card with a "process already retired" hint seconds after
 *  the user read the real answer. */
function applyDeletedEvent(evt: GlobalSseEvent): void {
  const pid = evt["process_id"];
  if (typeof pid !== "string") return;
  const action = resolvedAction.value;
  if (action === "list") {
    processList.value = processList.value.filter((p) => p.id !== pid);
    return;
  }
  if (action === "wait" && resultSnapshot.value !== null) return;
  if (info.value && info.value.id === pid) {
    info.value = null;
    retired.value = true;
  }
}

function subscribeSse(): void {
  try {
    disposeSse = connectGlobalEvents({
      onEvent(evt) {
        if (evt.type === "background_process.updated") {
          applyUpdatedEvent(evt);
        } else if (evt.type === "background_process.deleted") {
          applyDeletedEvent(evt);
        }
      },
      onError() {
        // EventSource auto-retries; we keep ``sseLive=true`` because the
        // browser will reconnect transparently.  We only flip the hint
        // when the SUBSCRIPTION itself failed to open (caught below).
      },
    });
  } catch {
    sseLive.value = false;
  }
}

// ---------------------------------------------------------------------------
// Mount / unmount
// ---------------------------------------------------------------------------

onMounted(() => {
  if (isErrorResult.value) {
    // Error result — no point in subscribing / probing; render the sentinel.
    return;
  }
  void refreshState();
  subscribeSse();
});

onBeforeUnmount(() => {
  if (disposeSse !== null) {
    disposeSse();
    disposeSse = null;
  }
});

// Re-probe whenever the resolved id changes (e.g. start action salvaging an
// id from a delayed result text).
watch(resolvedId, (id, prev) => {
  if (id !== null && id !== prev) {
    void refreshState();
  }
});

// ---------------------------------------------------------------------------
// User actions
// ---------------------------------------------------------------------------

async function onStop(id: string): Promise<void> {
  loading.value = true;
  try {
    const after = await stopBgp(id);
    if (after !== null) info.value = after;
    asyncError.value = "";
  } catch (err) {
    asyncError.value = String((err as Error).message ?? err);
  } finally {
    loading.value = false;
  }
}

async function onRestart(id: string): Promise<void> {
  loading.value = true;
  // Optimistic feedback: the HTTP ``restart`` blocks until the freshly spawned
  // process finishes its ready-probe (up to the manager's 30s ready window),
  // so without an immediate local transition the card sits on the old terminal
  // status while the button shows "刷新中…" — read by the user as "restart does
  // nothing". Flip the visible status to ``starting`` right away; the real Info
  // (and subsequent running→ready→exited transitions) then arrive via the HTTP
  // response below and the ``/api/events`` SSE stream, correcting this optimistic
  // guess (State-Truth-First: optimistic write is allowed only as instant
  // feedback, always reconciled by the authoritative source).
  if (info.value !== null) {
    info.value = {
      ...info.value,
      status: "starting",
      ready: false,
      exit_code: null,
      signal: null,
    };
  }
  try {
    const after = await restartBgp(id);
    if (after !== null) info.value = after;
    asyncError.value = "";
  } catch (err) {
    asyncError.value = String((err as Error).message ?? err);
  } finally {
    loading.value = false;
  }
}

async function onShowLogs(id: string): Promise<void> {
  if (logsOpen.value) {
    logsOpen.value = false;
    return;
  }
  // Open the panel IMMEDIATELY (before the async fetch) so the click has a
  // visible effect right away — the ``<pre>`` shows a "加载中…" placeholder
  // while ``fetchBgpLogs`` is in flight. Previously the panel only appeared
  // after the round-trip resolved, so a slow fetch read as "the Logs button
  // does nothing" (the reported "日志按钮无法点击" bug).
  logsOpen.value = true;
  loading.value = true;
  try {
    const logs = await fetchBgpLogs(id);
    logsText.value = logs?.output ?? "";
    asyncError.value = "";
  } catch (err) {
    asyncError.value = String((err as Error).message ?? err);
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

/** Map a manager status to one of ``info`` / ``success`` / ``warning`` /
 *  ``error`` so the badge can pick the right theme token. */
type StatusKind = "info" | "success" | "warning" | "error" | "muted";
function statusKind(s: BgpStatus | undefined, ready: boolean): StatusKind {
  if (s === undefined) return "muted";
  if (s === "ready" || (s === "running" && ready)) return "success";
  if (s === "running" || s === "starting") return "info";
  if (s === "stopping") return "warning";
  if (s === "failed") return "error";
  return "muted"; // stopped / exited
}

function statusLabel(s: BgpStatus | undefined): string {
  if (s === undefined) return "?";
  return t(`chat.backgroundProcess.bgpStatus.${s}`);
}

/** Is the status terminal (so Stop is meaningless)? */
function isTerminal(s: BgpStatus | undefined): boolean {
  return s === "exited" || s === "failed" || s === "stopped";
}

/** Truncate long command for compact list rendering (full command shown in
 *  single-process detail). */
function shortCommand(cmd: string, max = 80): string {
  if (cmd.length <= max) return cmd;
  return cmd.slice(0, max - 1) + "…";
}

/** Format ms-epoch as a local time string; empty for ``0`` (unset). */
function formatTime(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || ms === 0) return "—";
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return String(ms);
  }
}

/** Text to render inside the logs ``<pre>``. Shows a "加载中…" placeholder
 *  while a logs fetch is in flight (so the just-opened panel is never blank —
 *  see ``onShowLogs``), the captured output when present, and the empty-output
 *  sentinel otherwise. */
const logsDisplay = computed<string>(() => {
  if (logsText.value) return logsText.value;
  if (loading.value) return t("chat.backgroundProcess.statusLoading");
  return t("chat.backgroundProcess.logsEmpty");
});

/** Error code / message extracted from the result text for the error mode.
 *  The text starts with ``[tool_error] <message>`` or ``[guardrail_blocked]
 *  <reason>``; we surface the bracket as the "code" and the rest as the
 *  message. */
const errorBreakdown = computed<{ code: string; message: string }>(() => {
  const s = resultText.value.trim();
  const m = s.match(/^\[([^\]]+)\]\s*(.*)$/s);
  if (m) {
    return { code: m[1] ?? "", message: m[2] ?? "" };
  }
  return { code: "error", message: s };
});

/** Active single-process Info to render — the freshly fetched / event-updated
 *  value when we have one, else a placeholder assembled from the two weaker
 *  sources, strongest first: the snapshot parsed out of the tool result
 *  (``resultSnapshot`` — an actual observation the back end made) and only
 *  then the request args. Returns ``null`` when we have none of them.
 *
 *  The result snapshot must outrank the args-derived defaults: for ``wait``
 *  the args hold no command and the "starting" default is simply WRONG (the
 *  process has usually just exited), so trusting args there would make the
 *  card contradict the result the user can read one line below it. */
const placeholderInfo = computed<BgpInfo | null>(() => {
  if (retired.value) return null;
  if (info.value !== null) return info.value;
  if (resolvedAction.value === "list" || resolvedAction.value === "logs") return null;
  // Best-effort placeholder for ``start`` while the SSE event is still in
  // flight — uses args.command / args.workdir etc. (the model emitted them
  // verbatim, so they are reliable display values).
  const argCmd = props.args["command"];
  const argCwd = props.args["workdir"] ?? props.args["cwd"];
  const argDesc = props.args["description"];
  const snap = resultSnapshot.value;
  if (resolvedId.value === null && typeof argCmd !== "string" && snap === null) {
    return null;
  }
  return {
    id: resolvedId.value ?? "(starting…)",
    session_id: "",
    pid: snap?.pid ?? null,
    command: snap?.command ?? (typeof argCmd === "string" ? argCmd : ""),
    cwd: typeof argCwd === "string" ? argCwd : "",
    description: typeof argDesc === "string" ? argDesc : null,
    ports: [],
    status: snap?.status ?? ("starting" as BgpStatus),
    lifetime: "session" as const,
    ready: false,
    exit_code: null,
    signal: null,
    output: "",
    time: { started: 0, updated: 0, ended: null },
  };
});

/** Header label for the single-process card.
 *
 *  Every action gets its OWN title. Before this, ``start`` / ``status`` /
 *  ``stop`` / ``restart`` shared one label and every displayed field came from
 *  the live process state, so two tool calls against the same job rendered two
 *  cards that were identical character-for-character — the user read that as
 *  "the UI duplicated my process". The action is the one thing that genuinely
 *  differs between those calls, so it belongs in the title. */
const singleTitle = computed<string>(() => {
  switch (resolvedAction.value) {
    case "wait":
      return t("chat.backgroundProcess.titleWait");
    case "status":
      return t("chat.backgroundProcess.titleStatus");
    case "stop":
      return t("chat.backgroundProcess.titleStop");
    case "restart":
      return t("chat.backgroundProcess.titleRestart");
    default:
      return t("chat.backgroundProcess.titleSingle");
  }
});

/** Per-action header glyph — the second visual discriminator between cards for
 *  the same job (the title above is the first). Mirrors the glyph vocabulary
 *  ``ToolExecPanel`` already uses for stopped (■) / restarted (↻) outcomes so
 *  the two card families read the same way. */
const singleIcon = computed<string>(() => {
  switch (resolvedAction.value) {
    case "wait":
      return "⏳";
    case "status":
      return "🔍";
    case "stop":
      return "■";
    case "restart":
      return "↻";
    default:
      return "🚀";
  }
});

/** Header subtitle — the model-supplied description (or the command it ran),
 *  same source the exec card's subtitle uses (``args.description`` →
 *  ``args.command``), falling back to the live/observed command so a
 *  collapsed card still says WHICH process it is. ``null`` ⇒ slot omitted. */
const subtitleText = computed<string | null>(() => {
  const argDesc = props.args["description"];
  if (typeof argDesc === "string" && argDesc.trim() !== "") return argDesc;
  const info_ = placeholderInfo.value;
  if (info_ !== null) {
    if (info_.description !== null && info_.description.trim() !== "") {
      return info_.description;
    }
    if (info_.command.trim() !== "") return shortCommand(info_.command, 60);
  }
  const argCmd = props.args["command"];
  if (typeof argCmd === "string" && argCmd.trim() !== "") {
    return shortCommand(argCmd, 60);
  }
  return null;
});

/** Did the ``wait`` this card reports actually collect an outcome?
 *
 *  Two independent signals, and both must agree. The back end appends
 *  "(settled: false — budget elapsed…)" to the result when the wait budget
 *  expired with the process still alive, which is authoritative about THIS
 *  call. The status is the second check: a live process that is still running
 *  did not settle regardless of what the text says. */
const WAIT_UNSETTLED_PATTERN = /settled:\s*false/i;
const waitSettled = computed<boolean>(
  () =>
    !WAIT_UNSETTLED_PATTERN.test(resultText.value) &&
    isTerminal(placeholderInfo.value?.status),
);

/** Right-hand header info, shown in BOTH folded and expanded states so a
 *  collapsed row still carries the identifying facts: the PID and — for
 *  ``wait`` — whether that call collected an outcome. The latter is what makes
 *  two successive ``wait`` cards for one job read as a timeline rather than a
 *  duplicated card. */
const headerInfoText = computed<string>(() => {
  const parts: string[] = [];
  const info_ = placeholderInfo.value;
  if (info_ !== null && info_.pid !== null) parts.push(`pid ${info_.pid}`);
  if (resolvedAction.value === "wait") {
    parts.push(
      t(
        waitSettled.value
          ? "chat.backgroundProcess.waitSettled"
          : "chat.backgroundProcess.waitStillRunning",
      ),
    );
  }
  return parts.join(" · ");
});

// ---------------------------------------------------------------------------
// Collapse (shared contract with ``ToolExecPanel``; see the module docstring
// for why the auto-expand PARAMETER differs)
// ---------------------------------------------------------------------------

const ui = useUiStore();

/** The user/global fold preference.
 *
 *  Default ``true`` — a background process is long-lived, so its steady state
 *  is "folded, I know it is up". The topbar bulk choice, once the user has
 *  ever made one, wins over that default exactly as it does for exec cards. */
const collapsed = ref<boolean>(ui.toolCardsCollapsed ?? true);

/** Set once the user has expressed a per-card choice (header click) or the
 *  topbar broadcast has spoken. Freezes the automatic force-expand below so we
 *  never fight a deliberate fold. */
const userToggled = ref<boolean>(ui.toolCardsCollapsed !== null);

// Topbar "collapse/expand all tool cards" broadcast. Watching the monotonic
// tick (not the boolean) re-applies the same value after the user has toggled
// this card individually — identical wiring to ToolExecPanel.
watch(
  () => ui.toolCardsBroadcastTick,
  () => {
    collapsed.value = ui.toolCardsCollapsed as boolean;
    userToggled.value = true;
  },
);

function onHeaderClick(): void {
  collapsed.value = !collapsed.value;
  userToggled.value = true;
}

/** Did this process die in a way the user needs to look at?
 *
 *  ``failed`` is the manager's own abnormal-termination status; a non-zero
 *  ``exit_code`` covers a process that exited cleanly from the OS's point of
 *  view but reported an error. A user-requested ``stop`` is NOT abnormal. */
const isAbnormalExit = computed<boolean>(() => {
  const info_ = placeholderInfo.value;
  if (info_ === null) return false;
  if (info_.status === "failed") return true;
  return info_.exit_code !== null && info_.exit_code !== 0;
});

/** Display-only mask over ``collapsed``.
 *
 *  The ONLY automatic override is an abnormal exit, and only while the user
 *  has not taken control of this card. Deliberately NOT the exec rule
 *  ("running ⇒ always expanded"): a dev server runs for the whole session, so
 *  that rule would make this card impossible to fold. */
const effectiveCollapsed = computed<boolean>(() => {
  if (!userToggled.value && isAbnormalExit.value) return false;
  return collapsed.value;
});
</script>

<template>
  <!-- ── ok=false / tool_error sentinel ─────────────────────────────────────
       Deliberately NOT collapsible: an actionable error must never hide
       behind a fold (same rule ToolExecPanel applies to its inline
       SSL-error affordance). -->
  <div
    v-if="isErrorResult"
    class="bgp-card bgp-card--error"
    data-testid="bgp-card-error"
    role="alert"
  >
    <div class="bgp-card-header">
      <div class="bgp-header-left">
        <span class="bgp-card-icon" aria-hidden="true">⚠</span>
        <span class="bgp-card-title">{{ t("chat.backgroundProcess.titleError") }}</span>
      </div>
      <div class="bgp-header-right">
        <span class="bgp-card-code">{{ errorBreakdown.code }}</span>
      </div>
    </div>
    <div class="bgp-card-body">
      <pre class="bgp-card-error-message">{{ errorBreakdown.message }}</pre>
    </div>
  </div>

  <!-- ── action="list" ────────────────────────────────────────────────────── -->
  <div
    v-else-if="resolvedAction === 'list'"
    class="bgp-card"
    :class="{ 'bgp-card--collapsed': effectiveCollapsed }"
    data-testid="bgp-card-list"
  >
    <div
      class="bgp-card-header bgp-card-header--toggle"
      data-testid="bgp-header-list"
      role="button"
      tabindex="0"
      :aria-expanded="!effectiveCollapsed"
      @click="onHeaderClick"
      @keydown.enter.prevent="onHeaderClick"
      @keydown.space.prevent="onHeaderClick"
    >
      <div class="bgp-header-left">
        <span class="bgp-card-icon" aria-hidden="true">🚀</span>
        <span class="bgp-card-title">{{ t("chat.backgroundProcess.titleList") }}</span>
        <span class="bgp-card-count">{{ processList.length }}</span>
      </div>
      <div class="bgp-header-right">
        <button
          type="button"
          class="bgp-btn bgp-btn--ghost"
          :disabled="loading"
          :title="t('chat.backgroundProcess.actionRefresh')"
          @click.stop="refreshState(true)"
        >
          {{ loading ? t("chat.backgroundProcess.statusRefreshing") : t("chat.backgroundProcess.actionRefresh") }}
        </button>
        <span
          class="bgp-card-arrow"
          :class="{ 'bgp-card-arrow--collapsed': effectiveCollapsed }"
          :aria-label="effectiveCollapsed ? t('chat.expand') : t('chat.collapse')"
        >▼</span>
      </div>
    </div>
    <!-- Errors / degraded-transport hints sit OUTSIDE the fold: they are
         actionable, so a collapsed card must still show them. -->
    <div v-if="asyncError !== ''" class="bgp-async-error">{{ asyncError }}</div>
    <div v-if="!sseLive" class="bgp-sse-hint">{{ t("chat.backgroundProcess.sseDisabled") }}</div>
    <div v-if="!effectiveCollapsed" class="bgp-card-body">
      <p
        v-if="processList.length === 0 && !loading"
        class="bgp-card-empty"
      >
        {{ t("chat.backgroundProcess.emptyList") }}
      </p>
      <ul v-else class="bgp-list">
        <li
          v-for="p in processList"
          :key="p.id"
          class="bgp-list-row"
          :data-testid="`bgp-row-${p.id}`"
        >
          <span
            class="bgp-status-dot"
            :class="`bgp-status-dot--${statusKind(p.status, p.ready)}`"
            :title="statusLabel(p.status)"
            :aria-label="statusLabel(p.status)"
          />
          <span class="bgp-list-pid">[{{ p.pid ?? t("chat.backgroundProcess.fieldNoPid") }}]</span>
          <span class="bgp-list-id" :title="p.id">{{ p.id }}</span>
          <span class="bgp-list-command" :title="p.command">{{ shortCommand(p.command) }}</span>
          <span v-if="p.description" class="bgp-list-desc">— {{ p.description }}</span>
          <span v-if="p.ports.length > 0" class="bgp-list-ports">
            :{{ p.ports.join(",") }}
          </span>
          <span class="bgp-list-actions">
            <button
              type="button"
              class="bgp-btn bgp-btn--mini"
              :disabled="loading"
              :title="t('chat.backgroundProcess.actionLogs')"
              @click="onShowLogs(p.id)"
            >📄</button>
            <button
              v-if="!isTerminal(p.status)"
              type="button"
              class="bgp-btn bgp-btn--mini bgp-btn--danger"
              :disabled="loading"
              :title="t('chat.backgroundProcess.actionStop')"
              @click="onStop(p.id)"
            >🛑</button>
            <button
              type="button"
              class="bgp-btn bgp-btn--mini"
              :disabled="loading"
              :title="t('chat.backgroundProcess.actionRestart')"
              @click="onRestart(p.id)"
            >🔄</button>
          </span>
        </li>
      </ul>
      <pre
        v-if="logsOpen"
        class="bgp-logs"
        data-testid="bgp-logs-list"
      >{{ logsDisplay }}</pre>
    </div>
  </div>

  <!-- ── action="logs" ────────────────────────────────────────────────────── -->
  <div
    v-else-if="resolvedAction === 'logs'"
    class="bgp-card"
    :class="{ 'bgp-card--collapsed': effectiveCollapsed }"
    data-testid="bgp-card-logs"
  >
    <div
      class="bgp-card-header bgp-card-header--toggle"
      data-testid="bgp-header-logs"
      role="button"
      tabindex="0"
      :aria-expanded="!effectiveCollapsed"
      @click="onHeaderClick"
      @keydown.enter.prevent="onHeaderClick"
      @keydown.space.prevent="onHeaderClick"
    >
      <div class="bgp-header-left">
        <span class="bgp-card-icon" aria-hidden="true">📄</span>
        <span class="bgp-card-title">{{ t("chat.backgroundProcess.titleLogs") }}</span>
        <span v-if="resolvedId" class="bgp-card-code">{{ resolvedId }}</span>
      </div>
      <div class="bgp-header-right">
        <button
          type="button"
          class="bgp-btn bgp-btn--ghost"
          :disabled="loading"
          @click.stop="refreshState(true)"
        >
          {{ loading ? t("chat.backgroundProcess.statusRefreshing") : t("chat.backgroundProcess.actionRefresh") }}
        </button>
        <span
          class="bgp-card-arrow"
          :class="{ 'bgp-card-arrow--collapsed': effectiveCollapsed }"
          :aria-label="effectiveCollapsed ? t('chat.expand') : t('chat.collapse')"
        >▼</span>
      </div>
    </div>
    <div v-if="asyncError !== ''" class="bgp-async-error">{{ asyncError }}</div>
    <div v-if="!effectiveCollapsed" class="bgp-card-body">
      <pre class="bgp-logs">{{ logsDisplay }}</pre>
    </div>
  </div>

  <!-- ── single-process detail (start / status / stop / restart / wait) ──────
       Header carries everything needed to identify the call while folded:
       per-action glyph + per-action title + status badge + description
       subtitle on the left, pid / wait-outcome + Refresh + arrow on the
       right. That is what makes two cards for the SAME job distinguishable
       without expanding either. -->
  <div
    v-else
    class="bgp-card"
    :class="[
      placeholderInfo ? `bgp-card--${statusKind(placeholderInfo.status, placeholderInfo.ready)}` : '',
      { 'bgp-card--collapsed': effectiveCollapsed },
    ]"
    data-testid="bgp-card-single"
  >
    <div
      class="bgp-card-header bgp-card-header--toggle"
      data-testid="bgp-header-single"
      role="button"
      tabindex="0"
      :aria-expanded="!effectiveCollapsed"
      @click="onHeaderClick"
      @keydown.enter.prevent="onHeaderClick"
      @keydown.space.prevent="onHeaderClick"
    >
      <div class="bgp-header-left">
        <span class="bgp-card-icon" aria-hidden="true">{{ singleIcon }}</span>
        <span class="bgp-card-title">{{ singleTitle }}</span>
        <span
          v-if="placeholderInfo"
          class="bgp-status-badge"
          :class="`bgp-status-badge--${statusKind(placeholderInfo.status, placeholderInfo.ready)}`"
        >
          <span
            class="bgp-status-dot"
            :class="`bgp-status-dot--${statusKind(placeholderInfo.status, placeholderInfo.ready)}`"
            aria-hidden="true"
          />
          {{ statusLabel(placeholderInfo.status) }}
          <span
            v-if="placeholderInfo.ready && placeholderInfo.status === 'running'"
            class="bgp-status-ready"
          >({{ t("chat.backgroundProcess.fieldReady") }})</span>
        </span>
        <span
          v-if="subtitleText !== null"
          class="bgp-card-subtitle"
          data-testid="bgp-card-subtitle"
          :title="subtitleText"
        >{{ subtitleText }}</span>
      </div>
      <div class="bgp-header-right">
        <span
          v-if="headerInfoText !== ''"
          class="bgp-header-info"
          data-testid="bgp-header-info"
        >{{ headerInfoText }}</span>
        <button
          type="button"
          class="bgp-btn bgp-btn--ghost"
          :disabled="loading || resolvedId === null"
          @click.stop="refreshState(true)"
        >
          {{ loading ? t("chat.backgroundProcess.statusRefreshing") : t("chat.backgroundProcess.actionRefresh") }}
        </button>
        <span
          class="bgp-card-arrow"
          :class="{ 'bgp-card-arrow--collapsed': effectiveCollapsed }"
          :aria-label="effectiveCollapsed ? t('chat.expand') : t('chat.collapse')"
        >▼</span>
      </div>
    </div>

    <div v-if="asyncError !== ''" class="bgp-async-error">{{ asyncError }}</div>
    <div v-if="!sseLive" class="bgp-sse-hint">{{ t("chat.backgroundProcess.sseDisabled") }}</div>

    <div v-if="!effectiveCollapsed" class="bgp-card-body">
      <p
        v-if="placeholderInfo === null"
        class="bgp-card-empty"
      >
        <template v-if="loading && !retired">{{ t("chat.backgroundProcess.statusLoading") }}</template>
        <template v-else>{{ t("chat.backgroundProcess.actionRetired") }}</template>
      </p>

      <dl v-else class="bgp-detail">
        <div class="bgp-detail-row">
          <dt>{{ t("chat.backgroundProcess.columnId") }}</dt>
          <dd class="bgp-mono">{{ placeholderInfo.id }}</dd>
        </div>
        <div class="bgp-detail-row">
          <dt>{{ t("chat.backgroundProcess.columnPid") }}</dt>
          <dd>{{ placeholderInfo.pid ?? t("chat.backgroundProcess.fieldNoPid") }}</dd>
        </div>
        <div class="bgp-detail-row">
          <dt>{{ t("chat.backgroundProcess.columnCommand") }}</dt>
          <dd class="bgp-mono" :title="placeholderInfo.command">{{ placeholderInfo.command }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.cwd">
          <dt>{{ t("chat.backgroundProcess.columnCwd") }}</dt>
          <dd class="bgp-mono">{{ placeholderInfo.cwd }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.description">
          <dt>{{ t("chat.backgroundProcess.columnDescription") }}</dt>
          <dd>{{ placeholderInfo.description }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.ports.length > 0">
          <dt>{{ t("chat.backgroundProcess.columnPorts") }}</dt>
          <dd>{{ placeholderInfo.ports.join(", ") }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.exit_code !== null">
          <dt>{{ t("chat.backgroundProcess.columnExitCode") }}</dt>
          <dd>{{ placeholderInfo.exit_code }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.time.started > 0">
          <dt>{{ t("chat.backgroundProcess.columnStarted") }}</dt>
          <dd>{{ formatTime(placeholderInfo.time.started) }}</dd>
        </div>
        <div class="bgp-detail-row" v-if="placeholderInfo.time.updated > 0">
          <dt>{{ t("chat.backgroundProcess.columnUpdated") }}</dt>
          <dd>{{ formatTime(placeholderInfo.time.updated) }}</dd>
        </div>
      </dl>

      <div v-if="placeholderInfo && resolvedId" class="bgp-actions">
        <button
          type="button"
          class="bgp-btn"
          :disabled="loading"
          @click="onShowLogs(resolvedId)"
        >
          📄 {{ t("chat.backgroundProcess.actionLogs") }}
        </button>
        <button
          v-if="!isTerminal(placeholderInfo.status)"
          type="button"
          class="bgp-btn bgp-btn--danger"
          :disabled="loading"
          @click="onStop(resolvedId)"
        >
          🛑 {{ t("chat.backgroundProcess.actionStop") }}
        </button>
        <button
          type="button"
          class="bgp-btn"
          :disabled="loading"
          @click="onRestart(resolvedId)"
        >
          🔄 {{ t("chat.backgroundProcess.actionRestart") }}
        </button>
      </div>

      <pre
        v-if="logsOpen"
        class="bgp-logs"
        data-testid="bgp-logs-single"
      >{{ logsDisplay }}</pre>
    </div>
  </div>
</template>

<style scoped>
/* All colours go through theme tokens (AGENTS.md §3.10 / chat.css palette).
   Fallback values match chat.css's defaults so the card renders sensibly even
   if a theme token is missing. */

/* Geometry deliberately mirrors ``.tool-exec-panel`` / ``.tool-exec-header``
   in styles/chat/chat.css (border + radius + overflow hidden shell, header
   painted with --bg-tertiary at 7px/--space-3 padding, body padded
   separately). That is what makes a COLLAPSED card exactly one exec-card row
   tall: same paddings, same --text-sm title, and nothing else in the flow.
   The card must NOT carry its own padding/gap — the surrounding
   ``.message-tool-calls`` supplies the inter-card gap, same as for exec. */
.bgp-card {
  border: 1px solid var(--border, rgba(127, 127, 127, 0.25));
  border-radius: var(--radius-sm, 6px);
  overflow: hidden;
  background: var(--bg-secondary, rgba(127, 127, 127, 0.04));
  color: var(--text-primary, inherit);
  font-size: var(--text-base, 13px);
  line-height: 1.5;
  min-width: 320px;
  max-width: 100%;
  transition: border-color var(--transition, 0.15s ease);
}

.bgp-card--error  { border-color: var(--error,   #ef4444); }
.bgp-card--success { border-left: 3px solid var(--success, #34d399); }
.bgp-card--info    { border-left: 3px solid var(--accent,  #60a5fa); }
.bgp-card--warning { border-left: 3px solid var(--warning, #fbbf24); }
.bgp-card--muted   { border-left: 3px solid var(--border,  rgba(127, 127, 127, 0.45)); }

.bgp-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2, 8px);
  padding: 7px var(--space-3, 12px);
  background: var(--bg-tertiary, rgba(127, 127, 127, 0.06));
  border-bottom: 1px solid var(--border, rgba(127, 127, 127, 0.25));
  font-weight: 600;
  user-select: none;
  transition: background var(--transition, 0.15s ease);
}
/* The divider stays even when the card is header-only: ``.tool-exec-header``
   keeps its 1px bottom border while collapsed, and that border is part of the
   38.19px row. Dropping it here made the collapsed bgp card 1px shorter than a
   collapsed exec card. */
.bgp-card-header--toggle {
  cursor: pointer;
}
.bgp-card-header--toggle:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.12));
}

.bgp-header-left {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex: 1;
}
.bgp-header-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.bgp-card-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: var(--space-2, 8px) var(--space-3, 12px);
}

.bgp-card-icon { font-size: var(--text-base, 14px); line-height: 1; flex-shrink: 0; }
.bgp-card-title {
  flex: 0 0 auto;
  font-size: var(--text-sm, 12px);
  color: var(--text-primary, inherit);
  white-space: nowrap;
}
.bgp-card-count {
  font-weight: 400;
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
}
.bgp-card-code {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-weight: 400;
  font-size: 12px;
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
}

/* Description slot — the ellipsis target, so a long command shrinks instead of
   pushing the pid / Refresh / arrow off the row (same trick as
   ``.tool-exec-subtitle-text``). */
.bgp-card-subtitle {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 400;
  font-size: 12px;
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
}

.bgp-header-info {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-weight: 400;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, rgba(127, 127, 127, 0.85));
  white-space: nowrap;
}

/* Collapse arrow — same glyph, rotation and transition as
   ``.tool-exec-arrow`` so both card families animate identically. */
.bgp-card-arrow {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, rgba(127, 127, 127, 0.85));
  transition: transform var(--transition, 0.15s ease);
  display: inline-block;
  flex-shrink: 0;
}
.bgp-card-arrow--collapsed {
  transform: rotate(-90deg);
}

.bgp-card-empty {
  margin: 0;
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
  font-style: italic;
}

.bgp-card-error-message {
  margin: 0;
  padding: 8px;
  background: var(--bg-secondary, rgba(127, 127, 127, 0.05));
  border-radius: 4px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--error, #ef4444);
}

/* Rendered OUTSIDE the collapsible body (an actionable error must not hide),
   so these two carry the body's own horizontal padding themselves. */
.bgp-async-error,
.bgp-sse-hint {
  padding: 4px var(--space-3, 12px);
  font-size: 12px;
}
.bgp-async-error {
  color: var(--error, #ef4444);
}
.bgp-sse-hint {
  color: var(--warning, #fbbf24);
}

/* ── status badges / dots ─────────────────────────────────────────────── */
.bgp-status-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 500;
  flex-shrink: 0;
}
.bgp-status-badge--success { background: rgba(52, 211, 153, 0.15); color: var(--success, #16a34a); }
.bgp-status-badge--info    { background: rgba(96, 165, 250, 0.15); color: var(--accent, #2563eb); }
.bgp-status-badge--warning { background: rgba(251, 191, 36, 0.15); color: var(--warning, #d97706); }
.bgp-status-badge--error   { background: rgba(239, 68, 68, 0.15);  color: var(--error, #dc2626); }
.bgp-status-badge--muted   { background: rgba(127, 127, 127, 0.15); color: var(--text-secondary, rgba(127, 127, 127, 0.85)); }

.bgp-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
.bgp-status-dot--success { background: var(--success, #16a34a); }
.bgp-status-dot--info    { background: var(--accent, #2563eb); }
.bgp-status-dot--warning { background: var(--warning, #d97706); }
.bgp-status-dot--error   { background: var(--error, #dc2626); }
.bgp-status-dot--muted   { background: var(--border, rgba(127, 127, 127, 0.45)); }

.bgp-status-ready {
  font-weight: 400;
  font-size: 10px;
  opacity: 0.85;
  margin-left: 2px;
}

/* ── detail view (single-process) ─────────────────────────────────────── */
.bgp-detail {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
}
.bgp-detail-row {
  display: contents;
}
.bgp-detail-row > dt {
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
  font-weight: 500;
  font-size: 12px;
}
.bgp-detail-row > dd {
  margin: 0;
  word-break: break-all;
  min-width: 0;
}
.bgp-mono {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
}

/* ── list view ────────────────────────────────────────────────────────── */
.bgp-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bgp-list-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border, rgba(127, 127, 127, 0.18));
  font-size: 12px;
}
.bgp-list-row:last-child { border-bottom: none; }
.bgp-list-pid {
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
  flex-shrink: 0;
}
.bgp-list-id {
  font-family: var(--font-mono, ui-monospace, monospace);
  flex-shrink: 0;
}
.bgp-list-command {
  font-family: var(--font-mono, ui-monospace, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
}
.bgp-list-desc {
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
  flex-shrink: 0;
}
.bgp-list-ports {
  color: var(--accent, #2563eb);
  font-size: 11px;
  flex-shrink: 0;
}
.bgp-list-actions {
  display: inline-flex;
  gap: 4px;
  margin-left: auto;
  flex-shrink: 0;
}

/* ── buttons + action bar ─────────────────────────────────────────────── */
.bgp-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}
.bgp-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid var(--border, rgba(127, 127, 127, 0.35));
  border-radius: 4px;
  background: var(--bg-tertiary);
  color: var(--text-primary, inherit);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
}
.bgp-btn:hover:not(:disabled) {
  background: var(--bg-hover);
}
.bgp-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.bgp-btn--mini {
  padding: 2px 6px;
  font-size: 14px;
  border-color: transparent;
}

/* Header-embedded Refresh. Box copied from ``.tool-exec-copy-btn`` — that
   button is the TALLEST child of the exec header, so it is what sets its
   38.19px row, and matching it is what makes a collapsed bgp card exactly as
   tall as a collapsed exec card (measured in a browser, not inferred).
   ``border: none`` rather than a transparent colour: the ``.bgp-btn`` base's
   1px border still occupies box space, which made this 25.19px against the
   exec button's 23.19px and pushed the row 2px too tall. ``.btn-icon`` drops
   the border the same way. Spacing comes from ``.bgp-header-right``'s gap. */
.bgp-btn--ghost {
  border: none;
  background: transparent;
  color: var(--text-secondary, var(--text-muted, rgba(127, 127, 127, 0.85)));
  padding: 2px 5px;
  font-size: var(--text-sm, 12px);
  line-height: 1.6;
}
.bgp-btn--danger {
  border-color: var(--error, #ef4444);
  color: var(--error, #dc2626);
}
.bgp-btn--danger:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.08);
}

/* ── logs viewer ──────────────────────────────────────────────────────── */
.bgp-logs {
  margin: 4px 0 0 0;
  padding: 8px;
  max-height: 320px;
  overflow: auto;
  background: var(--bg-code, var(--bg-secondary, #1e1e1e));
  color: var(--text-code, var(--text-primary, #e4e4e7));
  border: 1px solid var(--border, rgba(127, 127, 127, 0.25));
  border-radius: 4px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
