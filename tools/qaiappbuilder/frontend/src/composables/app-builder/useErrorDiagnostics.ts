// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useErrorDiagnostics — App Builder 错误诊断面板的纯逻辑层。
 *
 * V1 真值（行为事实来源）：
 *   QAIModelBuilder_v1_pure/frontend/js/components/app-builder/DynamicOutput.js
 *   :236-275 (errorView computed) + :759-872 (错误模板)
 *
 * V1 把 6 段诊断字段（message / hint / stderr / runner logs / traceback /
 * spawn）从 `run.error` + `run.error.detail` + `run.logs` 投影出来，缺什么就
 * 不渲染什么。本 composable 用同样的口径把任意结构（V2 store 当前的
 * `error: string | null` + 可选的未来 `errorDetail`、加上父级已经从 frames
 * 里抽出来的 `runLogs`）规范成 `{ kind, title, body }` 的段列表，供模板
 * `v-for` 渲染。
 *
 * V2 优于 V1：
 *   - V1 是 inline plain JS + 模板里逐段 v-if + 6 个 boolean 控制展开，状态
 *     与渲染纠缠；这里抽成纯函数式投影 + 段元数据数组，模板只 `v-for`。
 *   - 提供类型化 props，缺字段一律产出 `undefined` 段并在投影时过滤。
 *   - 复制反馈（"Copied" 短闪）作为本 composable 的 reactive 状态返回，由
 *     单个 ref 计时管控；V1 用 `_flash()` 全局工具与组件耦合。
 */

import { computed, ref, type ComputedRef, type Ref } from "vue";

/** 单段诊断的语义标识（同时作为 i18n / DOM 标记 key）。 */
export type DiagnosticSectionKind =
  | "message"
  | "hint"
  | "stderr"
  | "logs"
  | "traceback"
  | "spawn";

/** 模板渲染所需的段元数据。`body` 永远是字符串（spawn 已 JSON.stringify）。 */
export interface DiagnosticSection {
  kind: DiagnosticSectionKind;
  title: string;
  body: string;
  /** 行数（用于 summary 显示 "n lines"），单行 message/hint 不显示。 */
  lineCount?: number;
  /** stderr 是否被后端截断到最后 500 行（V1 标记，缺则按 false）。 */
  truncated?: boolean;
}

/** 投影输入：所有字段都 optional，缺则该段不出现。 */
export interface ErrorDiagnosticsInput {
  /** 顶层主错误信息（V2 当前 `run.error` 即为此）。 */
  message?: string | null;
  /** 高层归因 hint（V1 `error.detail.crash_hint`，V2 store 暂未透传）。 */
  hint?: string | null;
  /** runner 的 stderr 片段（V1 `error.detail.stderr_lines.join("\n")`）。 */
  stderr?: string | null;
  /** runner streaming logs（父组件已从 frames 抽出的 `{stream,line}[]`）。 */
  runLogs?: readonly { stream: string; line: string }[] | null;
  /** python traceback 多行（V1 `error.traceback`）。 */
  traceback?: string | null;
  /** runner spawn 上下文（cwd / python_exe / PYTHONPATH / model_id 等）。 */
  spawn?: Record<string, unknown> | null;
  /** stderr 是否截断（V1 `error.detail.stderr_truncated`）。 */
  stderrTruncated?: boolean;
  /** 进程退出码（V1 `error.detail.exit_code`）。头部显示用，非诊断段。 */
  exitCode?: number | null;
  /** 进程退出码 hex（V1 `error.detail.exit_code_hex`，如 0xC0000005）。 */
  exitCodeHex?: string | null;
}

/** i18n 标题字典（由调用方注入，避免 composable 与 vue-i18n 直接耦合）。 */
export interface DiagnosticTitles {
  message: string;
  hint: string;
  stderr: string;
  logs: string;
  traceback: string;
  spawn: string;
}

/**
 * 规范化原始错误数据为段列表 + 提供复制操作 + 复制成功短闪反馈。
 *
 * `flashKind` 持有最近一次复制成功的段 kind（或 `"all"`），约 1.5s 后回退
 * 到 `null`，供模板把按钮文案/图标短暂切到 "Copied"。
 */
export function useErrorDiagnostics(
  input: Ref<ErrorDiagnosticsInput> | ComputedRef<ErrorDiagnosticsInput>,
  titles: Ref<DiagnosticTitles> | ComputedRef<DiagnosticTitles>,
): {
  sections: ComputedRef<DiagnosticSection[]>;
  /** 进程退出码（头部显示；缺则 null）。 */
  exitCode: ComputedRef<number | null>;
  /** 进程退出码 hex（头部显示；缺则 null）。 */
  exitCodeHex: ComputedRef<string | null>;
  flashKind: Ref<DiagnosticSectionKind | "all" | null>;
  copySection: (section: DiagnosticSection) => Promise<void>;
  copyAll: () => Promise<void>;
} {
  const flashKind = ref<DiagnosticSectionKind | "all" | null>(null);
  let flashTimer: ReturnType<typeof setTimeout> | null = null;

  function flash(kind: DiagnosticSectionKind | "all"): void {
    flashKind.value = kind;
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashKind.value = null;
      flashTimer = null;
    }, 1500);
  }

  /** 把 runLogs 数组拼成纯文本（与 stderr 同口径，供 <pre> 渲染 + 复制）。 */
  function joinLogs(
    logs: readonly { stream: string; line: string }[] | null | undefined,
  ): string {
    if (!Array.isArray(logs) || logs.length === 0) return "";
    return logs.map((l) => l.line).join("\n");
  }

  /**
   * V1 `_trimErrorMessage`（DynamicOutput.js:244-250）：后端 PROCESS_EXITED 的
   * message 末尾会拼一段 "--- stderr (last lines) ---\n<tail>" 以兼容只读
   * message 的客户端。当我们已经把 stderr 作为独立段渲染时，头部 message 去掉
   * 这段重复，只保留首行 summary（如 "runner exited with code ... before
   * emitting 'done'"）。没有独立 stderr 段时保留全文，避免吃掉唯一线索。
   */
  function trimMessage(raw: string, hasStderrSection: boolean): string {
    if (!raw) return "";
    if (!hasStderrSection) return raw;
    const marker = "--- stderr (last lines) ---";
    const idx = raw.indexOf(marker);
    if (idx >= 0) return raw.slice(0, idx).trimEnd();
    return raw;
  }

  const exitCode = computed<number | null>(() => {
    const v = input.value.exitCode;
    return typeof v === "number" ? v : null;
  });

  const exitCodeHex = computed<string | null>(() => {
    const v = input.value.exitCodeHex;
    return typeof v === "string" && v !== "" ? v : null;
  });

  const sections = computed<DiagnosticSection[]>(() => {
    const src = input.value;
    const t = titles.value;
    const out: DiagnosticSection[] = [];

    const stderr = (src.stderr ?? "").trim();
    const message = trimMessage((src.message ?? "").trim(), stderr !== "");
    if (message !== "") {
      out.push({ kind: "message", title: t.message, body: message });
    }

    const hint = (src.hint ?? "").trim();
    if (hint !== "") {
      out.push({ kind: "hint", title: t.hint, body: hint });
    }

    if (stderr !== "") {
      out.push({
        kind: "stderr",
        title: t.stderr,
        body: stderr,
        lineCount: stderr.split("\n").length,
        truncated: src.stderrTruncated === true,
      });
    }

    const logsText = joinLogs(src.runLogs);
    // V1 行为：当 runner logs 与 stderr 完全一致时不重复渲染（减少冗余）。
    if (logsText !== "" && logsText !== stderr) {
      out.push({
        kind: "logs",
        title: t.logs,
        body: logsText,
        lineCount: logsText.split("\n").length,
      });
    }

    const traceback = (src.traceback ?? "").trim();
    if (traceback !== "") {
      out.push({
        kind: "traceback",
        title: t.traceback,
        body: traceback,
        lineCount: traceback.split("\n").length,
      });
    }

    const spawn = src.spawn;
    if (spawn !== null && spawn !== undefined && Object.keys(spawn).length > 0) {
      let spawnText = "";
      try {
        spawnText = JSON.stringify(spawn, null, 2);
      } catch {
        spawnText = String(spawn);
      }
      out.push({
        kind: "spawn",
        title: t.spawn,
        body: spawnText,
        lineCount: spawnText.split("\n").length,
      });
    }

    return out;
  });

  async function writeClipboard(text: string): Promise<boolean> {
    if (text === "") return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  async function copySection(section: DiagnosticSection): Promise<void> {
    const ok = await writeClipboard(section.body);
    if (ok) flash(section.kind);
  }

  async function copyAll(): Promise<void> {
    // 拼成 markdown 形式：每段一个 ###；与 V1 `onCopyDiagnostics` 输出 JSON
    // 不同——V1 的 JSON dump 是粘到 issue 用的"全量录像"，本组件 "Copy
    // diagnostics" 按钮聚焦在"用户视图能看到的 6 段"——文本可读性更高，
    // 满足判据 2（用户视角对齐：复制后粘进 chat / IDE 即可阅读）。
    const list = sections.value;
    if (list.length === 0) return;
    const parts = list.map((s) => {
      // message 段没必要包成代码块；其它段都是多行原始文本，用 ``` 围栏。
      if (s.kind === "message" || s.kind === "hint") {
        return `### ${s.title}\n\n${s.body}\n`;
      }
      return `### ${s.title}\n\n\`\`\`\n${s.body}\n\`\`\`\n`;
    });
    const ok = await writeClipboard(parts.join("\n"));
    if (ok) flash("all");
  }

  return { sections, exitCode, exitCodeHex, flashKind, copySection, copyAll };
}
