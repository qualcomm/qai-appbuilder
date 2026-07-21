<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ErrorDiagnosticsPanel — App Builder 错误诊断面板（V1 行为对齐，V2 优良结构）。
 *
 * V1 真值（行为事实来源）：
 *   QAIModelBuilder_v1_pure/frontend/js/components/app-builder/DynamicOutput.js
 *   :759-872（错误模板 6 段独立可折叠 + 每段 ⧉ 复制 + 底部 Copy diagnostics）
 *
 * 6 段诊断（缺则不渲染）：
 *   1. message   顶部主错误信息（红字粗体；不放 <details> 直接展示）
 *   2. hint      高层归因 hint（OOM / not-installed / generic 的 crash hint）
 *   3. stderr    runner stderr 片段
 *   4. logs      runner streaming logs（与 stderr 不重复时才渲染）
 *   5. traceback python traceback
 *   6. spawn     runner spawn 上下文（cwd / python_exe / PYTHONPATH 等）
 *
 * 顶部还保留 V2 已有的 errorKind 分支（oom / not-installed / generic）：
 * 那是固定文案的 i18n hint 列表（与 message/hint 段并列、不冲突）。
 *
 * V2 优于 V1：
 *   - V1 是 inline plain JS + 模板里 6 个 `errorSegOpen.*` boolean + 6 个
 *     `<details v-if>`，状态与渲染纠缠；这里抽出 `useErrorDiagnostics`
 *     composable 做纯投影，模板只 `v-for sections`。
 *   - 类型化 props + composable 输入；缺字段（如 V2 store 暂无 errorDetail）
 *     时仍能正常工作，只显示能消费到的段。
 *   - 复制按钮的 "Copied" 反馈用 ref 计时管控，禁止 alert/confirm/prompt。
 *
 * 复用全局 CSS：`ab-output-error-*`（已有；定义在 styles/app-builder/app-builder.css）。
 */

import { computed, toRef } from "vue";
import { useI18n } from "vue-i18n";
import {
  useErrorDiagnostics,
  type DiagnosticTitles,
  type ErrorDiagnosticsInput,
} from "@/composables/app-builder/useErrorDiagnostics";

interface Props {
  /** 顶层主错误信息（V2 store 当前 `run.error`，可能为 null）。 */
  error?: string | null;
  /**
   * 结构化错误详情（V1 `error.detail`）。V2 store 暂未透传此字段；
   * 主 Agent 后续在 store 加 `errorDetail` 后由父组件传入即可生效。
   * 期望键：
   *   - hint / crash_hint / crashHint           → hint 段
   *   - stderr / stderr_lines (string[])        → stderr 段
   *   - traceback                               → traceback 段
   *   - spawn / spawn_context (Record)          → spawn 段
   *   - stderr_truncated (boolean)              → stderr 段截断标记
   */
  errorDetail?: Record<string, unknown> | null;
  /** errorKind 顶部建议（与 V2 当前实现一致；本组件保留这块固定文案块）。 */
  errorKind?: "oom" | "not-installed" | "generic";
  /** 父组件已从 frames 抽出的 runner logs；缺则该段不渲染。 */
  runLogs?: readonly { stream: string; line: string }[] | null;
}

const props = withDefaults(defineProps<Props>(), {
  error: null,
  errorDetail: null,
  errorKind: "generic",
  runLogs: null,
});

const emit = defineEmits<{
  /** 用户点击 "Send error to Chat" —— 父组件转发到 chat 桥。 */
  "send-error-to-chat": [];
}>();

const { t } = useI18n();

/**
 * 把宽松的 `errorDetail` 投影成 composable 期望的 `ErrorDiagnosticsInput`。
 * 兼容 V1 风格的下划线键（`stderr_lines` / `crash_hint` / `spawn_context` /
 * `stderr_truncated`）和 camelCase 键（`crashHint` 等），缺字段则该段消失。
 */
const diagInput = computed<ErrorDiagnosticsInput>(() => {
  const detail = (props.errorDetail ?? {}) as Record<string, unknown>;

  // hint：detail.hint > detail.crash_hint > detail.crashHint
  const hintRaw = detail.hint ?? detail.crash_hint ?? detail.crashHint;
  const hint = typeof hintRaw === "string" ? hintRaw : null;

  // stderr：detail.stderr (string) > detail.stderr_lines (string[]).join("\n")
  let stderr: string | null = null;
  if (typeof detail.stderr === "string") {
    stderr = detail.stderr;
  } else if (Array.isArray(detail.stderr_lines)) {
    stderr = detail.stderr_lines.map((s) => String(s)).join("\n");
  }

  // traceback：detail.traceback > detail.bootstrap_trace（V1 PROCESS_EXITED 用
  // bootstrap_trace 记录"崩溃前走到的最后一步"，stderr 为空时是唯一线索）。
  const traceback =
    typeof detail.traceback === "string"
      ? detail.traceback
      : typeof detail.bootstrap_trace === "string"
        ? detail.bootstrap_trace
        : null;

  const spawnRaw = detail.spawn ?? detail.spawn_context;
  const spawn =
    spawnRaw !== null && spawnRaw !== undefined && typeof spawnRaw === "object"
      ? (spawnRaw as Record<string, unknown>)
      : null;

  const stderrTruncated = detail.stderr_truncated === true;

  // exit_code / exit_code_hex（V1 error.detail.exit_code(_hex)）—— 头部显示用。
  const exitCodeRaw = detail.exit_code ?? detail.exitCode;
  const exitCode = typeof exitCodeRaw === "number" ? exitCodeRaw : null;
  const exitCodeHexRaw = detail.exit_code_hex ?? detail.exitCodeHex;
  const exitCodeHex =
    typeof exitCodeHexRaw === "string" && exitCodeHexRaw !== ""
      ? exitCodeHexRaw
      : null;

  return {
    message: props.error,
    hint,
    stderr,
    runLogs: props.runLogs,
    traceback,
    spawn,
    stderrTruncated,
    exitCode,
    exitCodeHex,
  };
});

/**
 * 段标题：复用既有 i18n key 兜底，新增 fallback 文案。如需独立 key 可由
 * 主 Agent 在 locales 增补（见组件底部报告）。当前直接用英文 fallback，
 * 与 V1 模板里硬编码的英文段标题一致（V1 这些是字面量、未走 i18n）。
 */
const titles = computed<DiagnosticTitles>(() => ({
  message: t("appBuilder.errorSection.message", "Error message"),
  hint: t("appBuilder.errorSection.hint", "Diagnostic hint"),
  stderr: t("appBuilder.errorSection.stderr", "stderr"),
  logs: t("appBuilder.errorSection.logs", "Runner logs"),
  traceback: t("appBuilder.errorSection.traceback", "Python traceback"),
  spawn: t("appBuilder.errorSection.spawn", "Spawn context"),
}));

const { sections, exitCode, exitCodeHex, flashKind, copySection, copyAll } =
  useErrorDiagnostics(diagInput, titles);

const inputRef = toRef(props, "error");
// 仅当存在 detail / runLogs 时才显示底部 "Copy diagnostics" + 段列表；
// 仅有 plain message 时退化为单条 message 段（仍出现在列表里）。
const hasDetail = computed<boolean>(() => sections.value.length > 0);

void inputRef.value; // 触达 prop 以确保 reactive 依赖被收集（lint 兜底）。
</script>

<template>
  <div
    class="ab-output-status-card ab-output-error-mode"
    data-status="error"
    role="alert"
  >
    <!-- 头部：图标 + 标签（保持与 V2 既有视觉一致） -->
    <div class="ab-output-error-head">
      <span
        class="ab-output-error-icon"
        aria-hidden="true"
      >✕</span>
      <span class="ab-output-error-code">[{{ t("appBuilder.statusError") }}]</span>
      <!-- exit code / hex（V1 DynamicOutput.js:766-769 对齐；缺则不显示） -->
      <span
        v-if="exitCode !== null"
        class="ab-output-error-rc"
      >
        exit={{ exitCode }}<template v-if="exitCodeHex"> ({{ exitCodeHex }})</template>
      </span>
    </div>

    <!-- errorKind 固定文案块（V2 既有；保留作为快速建议，与段独立） -->
    <div
      v-if="errorKind === 'oom'"
      class="ab-output-error-suggest ab-output-error-suggest--oom"
    >
      <span
        class="ab-output-error-suggest-icon"
        aria-hidden="true"
      >💾</span>
      <div class="ab-output-error-suggest-body">
        <strong>{{ t("appBuilder.errorHint.oom.title") }}</strong>
        <p>{{ t("appBuilder.errorHint.oom.desc") }}</p>
        <ul>
          <li>{{ t("appBuilder.errorHint.oom.tip1") }}</li>
          <li>{{ t("appBuilder.errorHint.oom.tip2") }}</li>
          <li>{{ t("appBuilder.errorHint.oom.tip3") }}</li>
        </ul>
      </div>
    </div>

    <div
      v-if="errorKind === 'not-installed'"
      class="ab-output-error-suggest ab-output-error-suggest--not-installed"
    >
      <span
        class="ab-output-error-suggest-icon"
        aria-hidden="true"
      >📦</span>
      <div class="ab-output-error-suggest-body">
        <strong>{{ t("appBuilder.errorHint.notInstalled.title") }}</strong>
        <p>{{ t("appBuilder.errorHint.notInstalled.desc") }}</p>
        <ul>
          <li>{{ t("appBuilder.errorHint.notInstalled.tip1") }}</li>
          <li>{{ t("appBuilder.errorHint.notInstalled.tip2") }}</li>
          <li>{{ t("appBuilder.errorHint.notInstalled.tip3") }}</li>
        </ul>
      </div>
    </div>

    <!-- 6 段诊断（V1 :759-872 对齐）：每段独立 <details>，缺则不渲染 -->
    <div
      v-if="hasDetail"
      class="ab-output-error-segments"
    >
      <template
        v-for="seg in sections"
        :key="seg.kind"
      >
        <!-- message / hint 是单行/短文，不放 <details>；用同样 seg 容器以
             复用 .ab-output-error-seg 视觉风格但默认展开且无折叠箭头。 -->
        <div
          v-if="seg.kind === 'message' || seg.kind === 'hint'"
          :class="[
            'ab-output-error-seg',
            seg.kind === 'message' ? 'ab-output-error-msg' : 'ab-output-error-hint',
          ]"
        >
          <div
            v-if="seg.kind === 'hint'"
            class="ab-output-error-hint-icon"
            aria-hidden="true"
          >
            💡
          </div>
          <div class="ab-output-error-seg-body">
            {{ seg.body }}
          </div>
          <button
            type="button"
            class="ab-output-error-seg-copy"
            :title="t('appBuilder.copyOutput')"
            @click.stop="copySection(seg)"
          >
            {{ flashKind === seg.kind ? t("appBuilder.copied") : "⧉" }}
          </button>
        </div>

        <!-- 多行段：可折叠 <details>，默认展开（V1 行为：errorSegOpen.* 初值） -->
        <details
          v-else
          class="ab-output-error-seg"
          :open="true"
        >
          <summary>
            <span class="ab-output-error-seg-title">
              {{ seg.title }}
              <template v-if="typeof seg.lineCount === 'number'">
                ({{ seg.lineCount }} lines<template v-if="seg.truncated">
                  · truncated to last 500
                </template>)
              </template>
            </span>
            <button
              type="button"
              class="ab-output-error-seg-copy"
              :title="t('appBuilder.copyOutput')"
              @click.stop="copySection(seg)"
            >
              {{ flashKind === seg.kind ? t("appBuilder.copied") : "⧉" }}
            </button>
          </summary>
          <pre class="ab-output-error-seg-body">{{ seg.body }}</pre>
        </details>
      </template>
    </div>

    <!-- 底部操作：Send to Chat + Copy diagnostics（一键复制全部段） -->
    <div class="ab-output-error-actions">
      <button
        type="button"
        class="ab-btn-sm primary"
        :title="t('appBuilder.sendErrorToChatTip')"
        @click="emit('send-error-to-chat')"
      >
        <span aria-hidden="true">↗</span>
        {{ t("appBuilder.sendErrorToChat") }}
      </button>
      <button
        type="button"
        class="ab-btn-sm"
        :disabled="!hasDetail"
        @click="copyAll"
      >
        <span aria-hidden="true">⧉</span>
        {{ flashKind === "all" ? t("appBuilder.copied") : t("appBuilder.copyDiagnostics") }}
      </button>
    </div>
  </div>
</template>
