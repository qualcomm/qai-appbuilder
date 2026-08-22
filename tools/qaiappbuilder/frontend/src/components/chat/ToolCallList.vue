<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ToolCallList — 主 / 子 Agent 共用的「一组工具调用」渲染组件。
 *
 * 此前主 Agent（ChatMessageList.vue）和子 Agent（SubAgentBlock.vue）各写了一遍
 * 「遍历工具调用 → todowrite 渲染 TaskListCard / 其余渲染 ToolExecPanel」的逻辑，
 * 两套 wire 字段名不同（`tool` vs `name`、`output` vs `result`、`durationMs` vs
 * `duration_ms`）导致重复。本组件把这段渲染抽成 **单一** 子组件，两处都通过一个
 * 轻量 mapper 把各自的数据归一化成 `ToolCallView[]`（统一视图模型）再传进来，从而
 * 「一套渲染逻辑两处复用」——做到子 Agent 工具卡与主 Agent 像素级一致（时长 / 参数
 * / 输出 / 徽标 / todowrite 任务卡 / 状态映射全部走同一条路径）。
 *
 * 不持有任何业务状态：`ToolExecPanel` / `TaskListCard` 本身仍是稳定蓝本，本组件只
 * 负责「分发 + 透传」，prompt 快照按钮事件原样冒泡给父组件。
 */
import { computed } from "vue";
import type { ToolCallView } from "@/stores/_chatTabsTypes";
import ToolExecPanel from "@/components/chat/ToolExecPanel.vue";
import TaskListCard from "@/components/chat/TaskListCard.vue";
import ChatQuestionCard from "@/components/chat/ChatQuestionCard.vue";
import BackgroundProcessCard from "@/components/chat/BackgroundProcessCard.vue";
import ConfigReviewCard from "@/components/chat/ConfigReviewCard.vue";
import StepProgressCard from "@/components/chat/StepProgressCard.vue";
import PipelineCard from "@/components/chat/PipelineCard.vue";

const props = defineProps<{
  /** 归一化后的工具调用视图数组（主 / 子 Agent 各自 map 而来）。 */
  calls: ToolCallView[];
  /** 是否显示 prompt 快照按钮（主 Agent 传，子 Agent 不传 ⇒ false）。 */
  showPromptButton?: boolean;
  /** 触发此组工具调用的 request_id（主 Agent 传，子 Agent 不传）。 */
  requestId?: string;
  /** 所属 tab id（主 Agent 传）——透传给 ChatQuestionCard 以便提交答案。
   *  子 Agent 不传 ⇒ 问答卡只读（子 Agent 不在前端交互作答）。 */
  tabId?: string;
  /** 仅渲染 active（待作答）问答卡：当「工具消息显示」开关关闭、但本组含一张
   *  待作答问答卡而必须强制显示时由父组件传 true。此时过滤掉其它工具卡，让
   *  开关对其余工具仍生效（只放行那张会阻塞回路的 active 问答卡）。 */
  activeQuestionOnly?: boolean;
  /** active（待作答）问答卡的 frame id（= tab.pendingQuestion.frameId），
   *  用于在 `activeQuestionOnly` 时精确筛出该张卡（key 即 frame id）。 */
  activeQuestionFrameId?: string;
}>();

/** 实际要渲染的工具调用：常规情况下全渲染；`activeQuestionOnly` 时只保留
 *  与 `activeQuestionFrameId` 匹配的那张 question 卡（其余隐藏，保证开关
 *  对其它工具仍生效，仅强制放行会阻塞回路的待作答问答卡）。 */
const visibleCalls = computed<ToolCallView[]>(() => {
  if (!props.activeQuestionOnly) return props.calls;
  const fid = props.activeQuestionFrameId ?? "";
  return props.calls.filter(
    (call) => call.toolName === "question" && String(call.key) === fid,
  );
});

const emit = defineEmits<{
  /** 透传 ToolExecPanel 的「打开 prompt 快照」事件给父组件。 */
  (e: "open-prompt-snapshot", requestId: string): void;
  /** 「按单个工具调用取消」（per-call cancel）：用户点了某张工具卡上的停止
   *  按钮。带上该 call 的 `callId`，父组件调 `store.cancelToolCall(tabId, callId)`
   *  → 后端只取消这一个工具、合成「已取消」结果喂回模型、当前回合继续。
   *  callId 缺失（如子 Agent 未回填 tool_call_id）时不发，避免误取消。 */
  (e: "cancel-tool", callId: string): void;
  /** 工具卡内「关闭 TLS 校验并重试」按钮被点击（仅 error_code ===
   *  "chat.llm.tls_cert_untrusted" 的卡片会渲染该按钮）。父组件据此复用与 LLM
   *  回合级错误完全相同的执行器（确认弹窗 → 关全局 ssl_verify → 重跑本回合）。 */
  (e: "configure-tls"): void;
  /** 透传 ToolExecPanel 的「打开定时任务所属会话」事件给父组件。 */
  (e: "open-conversation", conversationId: string): void;
}>();

function onOpenPromptSnapshot(requestId: string): void {
  emit("open-prompt-snapshot", requestId);
}

function onStopCall(callId: string | undefined): void {
  const cid = (callId ?? "").trim();
  if (cid) {
    emit("cancel-tool", cid);
  }
}

function onConfigureTls(): void {
  emit("configure-tls");
}

function onOpenConversation(conversationId: string): void {
  emit("open-conversation", conversationId);
}

/** Pluck a string ``args.action`` off a tool call's argument record without
 *  fighting the ``unknown`` index type — used by the background_process
 *  branch below.  Defaults to ``"unknown"`` so the dedicated card can still
 *  pick a sensible single-process layout (it has its own fallback chain). */
function readAction(args: Record<string, unknown>): string {
  const a = args["action"];
  return typeof a === "string" ? a : "unknown";
}

/** Whether a ``background_process`` call should render the rich interactive
 *  card vs. fall through to the generic ``ToolExecPanel``.
 *
 *  Rationale (fixes the "one process shows many cards" bug): in an agentic
 *  loop the LLM commonly polls ``status`` (and calls ``logs`` / ``stop`` /
 *  ``restart``) many times for the SAME process within a single turn. Rendering
 *  a full state card for each of those read-only probes floods the transcript
 *  with near-identical "启动中 / PID —" cards that the user reads as "one
 *  process shown many times".
 *
 *  Only ``start`` (spawns a NEW process — the moment a rich card is genuinely
 *  informative), ``wait`` (the blocking RESULT-DELIVERY point: it returns the
 *  final status together with the captured output, and the user needs the live
 *  status / PID / logs affordance during the tens of seconds it blocks) and
 *  ``list`` (the multi-process overview, which has no ToolExecPanel
 *  equivalent) keep the rich card. Every other action — the read-only /
 *  mutating probes ``status`` / ``logs`` / ``stop`` / ``restart`` (and any
 *  ``unknown``) — renders as a plain tool row instead, so the user sees "the
 *  model queried the process" without a wall of duplicate cards.
 *
 *  ``wait`` is deliberately NOT de-duplicated across calls: unlike ``status``
 *  (repeated sampling of one fact), successive ``wait`` calls form a TIMELINE
 *  — each ``settled: false`` means "the budget elapsed, still running", so the
 *  number of cards is itself the evidence of how long the model waited.
 *  Collapsing them would erase that. The 120 s cap keeps the count small.
 *
 *  The authoritative live state still shows on the ``start`` / ``wait`` cards
 *  (and the operator sidebar), which subscribe to the ``/api/events`` SSE
 *  stream. */
function isRichBackgroundProcess(args: Record<string, unknown>): boolean {
  const action = readAction(args);
  return action === "start" || action === "wait" || action === "list";
}
</script>

<template>
  <template
    v-for="call in visibleCalls"
    :key="call.key"
  >
    <!-- todowrite 渲染专用任务清单快照卡（V2 增强），其余渲染通用工具卡。 -->
    <TaskListCard
      v-if="call.toolName === 'todowrite'"
      :args="call.args"
    />
    <!-- question 渲染内嵌可分页问答卡（V2 增强）：作答 / 审查 / 已作答只读三态。 -->
    <ChatQuestionCard
      v-else-if="call.toolName === 'question'"
      :args="call.args"
      :result="call.result"
      :frame-id="String(call.key)"
      :tab-id="tabId"
    />
    <!-- background_process 渲染富交互卡（V2 增强）：仅 start（新建进程）/ wait
         （阻塞收口，返回最终状态 + 输出，阻塞期间用户需要实时状态/PID/日志）/
         list（进程总览）三种动作值得独占富卡；status / logs / stop / restart 这类
         对同一进程的只读探测 / 操作在 agent 循环里会被高频调用，若每次都渲染富卡
         会用近乎相同的「启动中 / PID —」卡片刷屏（用户感知为「一个进程显示很多
         次」）。故其余动作退化到通用 ToolExecPanel（下方 v-else），只留一条工具行。
         wait 有意不做同 id 折叠：与 status（同一事实的重复采样）不同，多次 wait
         构成一条时间线（每个 settled:false 表示「预算用完仍在跑」），卡片数量本身
         就是「模型等了多久」的证据，折叠会把它抹掉；120 秒上限保证数量不失控。
         真实实时状态由 start / wait 卡 + 运维 sidebar 通过 /api/events SSE 呈现。 -->
    <BackgroundProcessCard
      v-else-if="call.toolName === 'background_process' && isRichBackgroundProcess(call.args)"
      :args="call.args"
      :result="call.result"
      :action="readAction(call.args)"
    />
    <!-- config_review 是 mapper 合成的 tool_call（MB Pro 的 config_review_needed
         事件），走独立卡以便结构化呈现（平台/模型/参数/倒计时一目了然），
         而不是被通用 ToolExecPanel 的滚动折叠区淹没。 -->
    <ConfigReviewCard
      v-else-if="call.toolName === 'config_review'"
      :args="call.args"
    />
    <!-- show_step_progress 渲染进度表格卡（MB Pro 任务进度可视化） -->
    <StepProgressCard
      v-else-if="call.toolName === 'show_step_progress'"
      :result="call.result"
    />
    <!-- show_pipeline 渲染依赖图卡（markdown 渲染，保留 file:line 证据） -->
    <PipelineCard
      v-else-if="call.toolName === 'show_pipeline'"
      :result="call.result"
    />
    <ToolExecPanel
      v-else
      :tool-name="call.toolName"
      :args="call.args"
      :result="call.result"
      :status="call.status"
      :output-size="call.outputSize"
      :truncated="call.truncated"
      :timestamp="call.timestamp"
      :duration-ms="call.durationMs"
      :args-streaming="call.argsStreaming"
      :args-char-count="call.argsCharCount"
      :timed-from-generation="call.timedFromGeneration"
      :generation-started-at="call.generationStartedAt"
      :total-ms="call.totalMs"
      :request-id="requestId ?? ''"
      :show-prompt-button="showPromptButton ?? false"
      :can-cancel="!!call.callId"
      :error-code="call.errorCode"
      @open-prompt-snapshot="onOpenPromptSnapshot"
      @stop="onStopCall(call.callId)"
      @configure-tls="onConfigureTls"
      @open-conversation="onOpenConversation"
    />
  </template>
</template>
