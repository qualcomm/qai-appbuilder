<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelCard — App Builder gallery card (V1 `ModelCard.js` parity).
 *
 * Renders a single on-device model as a selectable card showing name +
 * variant-count badge + featured star + category + IO + runtime + latency/mem
 * + a status dot. Click selects; double-click / right-click / `i` opens the
 * info drawer.
 *
 * Consumes the ready-made `.ab-model-card*` classes from
 * `styles/app-builder/app-builder.css` (real design tokens, no scoped CSS).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { AppModelCardVM } from "./types";

interface Props {
  model: AppModelCardVM;
  selected?: boolean;
}

const props = withDefaults(defineProps<Props>(), { selected: false });

const emit = defineEmits<{
  select: [modelId: string];
  info: [model: AppModelCardVM];
}>();

const { t } = useI18n();

const variantsN = computed<number>(() => props.model.variants?.length ?? 0);
const hasMultiVariants = computed<boolean>(() => variantsN.value >= 2);

const latency = computed<string | null>(() => {
  const v = Number(props.model.metrics?.latencyMs);
  return Number.isFinite(v) ? t("appBuilder.variant.latencyHint", { n: Math.round(v) }) : null;
});
const mem = computed<string | null>(() => {
  const v = Number(props.model.metrics?.memoryMB);
  // V1 ModelCard renders memory as `{n}MB` (no space; ModelCard.js:28).
  // The spaced `variant.sizeOnDisk` ("{n} MB") stays for the info drawer.
  return Number.isFinite(v) ? t("appBuilder.variant.cardSize", { n: Math.round(v) }) : null;
});

const runtimeText = computed<string>(() => {
  const r = props.model.runtime ?? {};
  const parts: string[] = [];
  if (r.backend) parts.push(String(r.backend).toUpperCase());
  if (r.delegate) parts.push(String(r.delegate).toUpperCase());
  if (hasMultiVariants.value) {
    parts.push(t("appBuilder.variant.countSuffix", { n: variantsN.value }));
  } else if (r.quantization) {
    parts.push(String(r.quantization).toUpperCase());
  }
  return parts.join(" · ");
});

const ioText = computed<string>(() => {
  const i = props.model.inputSchema?.kind || "?";
  const o = props.model.outputSchema?.kind || "?";
  return `${i} → ${o}`;
});

const status = computed<string>(() => props.model.status || "Ready");
const statusClass = computed<string>(() => {
  switch (status.value) {
    case "Ready":
      return "status-ready";
    case "NotInstalled":
      return "status-notinstalled";
    case "Updating":
    case "Downloading":
      return "status-loading";
    case "Error":
      return "status-error";
    default:
      return "status-unknown";
  }
});

// V1 deps-status 逐 pack 进度 parity (useAppBuilderRegistry.js:287-309):
// surface the live dependency-install progress as a small badge so the user
// sees "正在安装依赖 / 缺少依赖" the moment a freshly-dropped Pack is probed.
// `depsStatus === 'ready'` (or absent) shows nothing — the regular install
// status dot already conveys readiness.
const depsBadge = computed<{ label: string; cls: string; title: string } | null>(() => {
  const ds = props.model.depsStatus;
  if (ds === "installing") {
    return {
      label: t("appBuilder.deps.installing"),
      cls: "ab-deps-badge--installing",
      title: t("appBuilder.deps.installingTitle"),
    };
  }
  if (ds === "missing") {
    // Prefer the backend pip-error hint (errorHint) as the tooltip so the
    // user gets the actionable fix (TLS / network / no_match / ...); fall
    // back to the missing-package list, then a generic message.
    const hint =
      props.model.depsErrorHint ||
      (props.model.depsMissing && props.model.depsMissing.length > 0
        ? t("appBuilder.deps.missingTitle", {
            pkgs: props.model.depsMissing.join(", "),
          })
        : t("appBuilder.deps.missingTitleGeneric"));
    return {
      label: t("appBuilder.deps.missing"),
      cls: "ab-deps-badge--missing",
      title: hint,
    };
  }
  return null;
});

function onClick(): void {
  emit("select", props.model.modelId);
}
function onInfo(e?: Event): void {
  e?.preventDefault();
  emit("info", props.model);
}
function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    onClick();
  } else if (e.key === "i" || e.key === "I") {
    e.preventDefault();
    onInfo();
  }
}
</script>

<template>
  <div
    class="ab-model-card"
    :class="{ selected, featured: model.featured }"
    role="option"
    tabindex="0"
    :aria-selected="selected ? 'true' : 'false'"
    :title="model.description || model.modelId"
    @click="onClick"
    @dblclick="onInfo"
    @contextmenu="onInfo"
    @keydown="onKeydown"
  >
    <div class="ab-model-card-header">
      <span class="ab-model-card-name">{{ model.displayName || model.modelId }}</span>
      <span
        v-if="hasMultiVariants"
        class="ab-model-card-variants-badge"
        :title="t('appBuilder.variant.countSuffix', { n: variantsN })"
        aria-hidden="true"
      >&times; {{ variantsN }}</span>
      <span
        v-if="model.featured"
        class="ab-model-card-star"
        :aria-label="t('appBuilder.aria.featured')"
      >&#9733;</span>
    </div>
    <div class="ab-model-card-line ab-model-card-cat">
      {{ model.category || "—" }}
    </div>
    <div class="ab-model-card-line ab-model-card-io">
      {{ ioText }}
    </div>
    <div
      v-if="runtimeText"
      class="ab-model-card-line ab-model-card-runtime"
    >
      {{ runtimeText }}
    </div>
    <div
      v-if="latency || mem"
      class="ab-model-card-line ab-model-card-metrics"
    >
      <span v-if="latency">{{ latency }}</span>
      <span v-if="latency && mem"> · </span>
      <span v-if="mem">{{ mem }}</span>
    </div>
    <div class="ab-model-card-status">
      <span
        class="ab-status-dot"
        :class="statusClass"
        aria-hidden="true"
      ></span>
      <span class="ab-status-label">{{ status }}</span>
      <span
        v-if="depsBadge"
        class="ab-deps-badge"
        :class="depsBadge.cls"
        :title="depsBadge.title"
      >{{ depsBadge.label }}</span>
    </div>
  </div>
</template>
