<script setup lang="ts">
/**
 * Settings → Search panel.
 *
 * Extracted from AgentSettingsPanel's "Search Source" group, which had grown
 * far past what belongs inside a tab about agent-loop tuning: two routing
 * switches, a per-engine health list, manual overrides and API-key management
 * for 21 engines. It is its own concern, so it gets its own tab.
 *
 * Layout, in the order a user actually needs it:
 *   1. Routing — CEBot vs Web, and which one answers by default.
 *   2. Free engines — every keyless engine, ordered by MEASURED quality
 *      (backend `priority_hint`, never a hardcoded frontend list), each with a
 *      single on/off switch. These all run CONCURRENTLY on every search and the
 *      hits are merged, so "off" means "stop querying this source".
 *   3. API-key engines — collapsed to the ones that already have a key; the
 *      rest hide behind a "show N more" toggle mirroring the model list.
 *
 * The engine roster is always fetched from GET /api/search/engines — the panel
 * never hardcodes engine ids, names, or order.
 */
import { computed, reactive, ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { type AppConfig } from "@/composables/useConfig";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const toast = useToast();
const { config: forgeConfig, save: saveConfig } = useForgeConfig();

// ─── Routing (persists to forge_config.chat.search, per-turn on the backend) ──
type SearchMode = "cebot_only" | "web_only" | "both";
type SearchDefaultProvider = "cebot" | "web" | "auto";

const SEARCH_MODES: readonly SearchMode[] = ["cebot_only", "web_only", "both"];
const SEARCH_DEFAULT_PROVIDERS: readonly SearchDefaultProvider[] = [
  "cebot",
  "web",
  "auto",
];

const searchForm = reactive<{
  mode: SearchMode;
  default_provider: SearchDefaultProvider;
  // `mode` must match the backend default in
  // `_chat_web_search_tool_bridge._resolve_search_plan` (web_only): if the panel
  // showed "Both" while the backend queried web only, the displayed setting
  // would be a lie until the user saved something.
}>({ mode: "web_only", default_provider: "auto" });

const rawChat = computed<Record<string, unknown>>(() => {
  const chat = (forgeConfig.value as Record<string, unknown> | null)?.chat;
  return chat != null && typeof chat === "object"
    ? (chat as Record<string, unknown>)
    : {};
});

function syncSearchForm(): void {
  const raw = rawChat.value.search;
  if (raw == null || typeof raw !== "object") return;
  const s = raw as Record<string, unknown>;
  if (s.mode === "cebot_only" || s.mode === "web_only" || s.mode === "both") {
    searchForm.mode = s.mode;
  }
  if (
    s.default_provider === "cebot" ||
    s.default_provider === "web" ||
    s.default_provider === "auto"
  ) {
    searchForm.default_provider = s.default_provider;
  }
}

const searchSaving = ref(false);

// The whole chat object is round-tripped so the backend's shallow top-level
// merge keeps sibling chat sub-keys (compaction / hooks) intact.
async function persistSearch(): Promise<void> {
  if (searchSaving.value) return;
  searchSaving.value = true;
  const nextSearch = {
    mode: searchForm.mode,
    default_provider: searchForm.default_provider,
  };
  try {
    await saveConfig({
      chat: { ...rawChat.value, search: nextSearch },
    } as Partial<AppConfig>);
  } catch (e) {
    toast.error(
      `${t("chat.search.advanced.loadFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    searchSaving.value = false;
  }
}

function onSearchModeChange(mode: SearchMode): void {
  searchForm.mode = mode;
  void persistSearch();
}

function onSearchDefaultProviderChange(provider: SearchDefaultProvider): void {
  searchForm.default_provider = provider;
  void persistSearch();
}

// ─── Engine roster (GET /api/search/engines) ─────────────────────────────────
type EngineManualState = "auto" | "forced_on" | "forced_off";
type EngineStatus = "enabled" | "disabled" | "needs_config";

interface SearchEngineItem {
  engine_id: string;
  display_name: string;
  engine_type: string;
  requires_credential: boolean;
  credential_key: string;
  enabled_by_default: boolean;
  description_i18n_key: string;
  priority_hint: number;
  score: number;
  manual_state: EngineManualState;
  success_rate: number;
  status: EngineStatus;
  quota_usage?: number | null;
  quota_limit?: number | null;
  quota_remaining?: number | null;
  quota_exhausted?: boolean | null;
}

const engines = ref<SearchEngineItem[]>([]);
const enginesLoading = ref(false);
const enginesError = ref(false);
const engineBusy = ref<string | null>(null);

async function loadEngines(): Promise<void> {
  enginesLoading.value = true;
  enginesError.value = false;
  try {
    const res = await apiJson<{ engines: SearchEngineItem[] }>(
      "GET",
      "/api/search/engines",
    );
    engines.value = res.engines;
  } catch {
    engines.value = [];
    enginesError.value = true;
  } finally {
    enginesLoading.value = false;
  }
}

onMounted(() => {
  syncSearchForm();
  void loadEngines();
});

// Backend `description_i18n_key` is "search.engines.<id>.desc"; the chat locale
// namespace needs a "chat." prefix for t().
function engineDescription(item: SearchEngineItem): string {
  return t(`chat.${item.description_i18n_key}`);
}

// ─── Free / keyed split, ordered by the backend's measured-quality hint ──────
// `priority_hint` is the single source of truth (its keyless band is ordered by
// measured relevance / authority / snippet / uniqueness / latency). Sorting here
// rather than re-listing engine ids keeps the frontend from drifting out of sync
// the moment the measurement is redone.
function byPriority(a: SearchEngineItem, b: SearchEngineItem): number {
  return b.priority_hint - a.priority_hint;
}

const freeEngines = computed(() =>
  engines.value.filter((e) => !e.requires_credential).sort(byPriority),
);

const paidEngines = computed(() =>
  engines.value.filter((e) => e.requires_credential).sort(byPriority),
);

// Keyed engines that already have a credential lead; the unconfigured rest are
// collapsed. Derived from `status`, never a hardcoded id list, so it stays right
// as keys are added or removed.
const paidConfigured = computed(() =>
  paidEngines.value.filter((e) => e.status !== "needs_config"),
);
const paidUnconfigured = computed(() =>
  paidEngines.value.filter((e) => e.status === "needs_config"),
);

const paidExpanded = ref(false);
const visiblePaidEngines = computed(() =>
  paidExpanded.value
    ? [...paidConfigured.value, ...paidUnconfigured.value]
    : paidConfigured.value,
);
const paidHiddenCount = computed(() =>
  paidExpanded.value ? 0 : paidUnconfigured.value.length,
);

// ─── Quality tier badge ──────────────────────────────────────────────────────
// Derived from the engine's rank WITHIN the free group rather than from an
// absolute score, so the badges stay meaningful after a re-measure reshuffles
// the priority band.
function qualityTier(item: SearchEngineItem): "high" | "medium" | "low" | null {
  if (item.requires_credential) return null;
  const index = freeEngines.value.findIndex((e) => e.engine_id === item.engine_id);
  if (index < 0) return null;
  const total = freeEngines.value.length;
  if (index < Math.ceil(total / 3)) return "high";
  if (index < Math.ceil((total * 2) / 3)) return "medium";
  return "low";
}

function qualityTierLabel(tier: "high" | "medium" | "low"): string {
  if (tier === "high") return t("chat.search.quality.tierHigh");
  if (tier === "medium") return t("chat.search.quality.tierMedium");
  return t("chat.search.quality.tierLow");
}

// ─── Per-engine on/off ───────────────────────────────────────────────────────
// Mapped onto the existing manual_state API: forced_off is a hard "never query
// this", anything else means the aggregator may schedule it. Switching back ON
// restores `auto` (health-score scheduling) rather than forcing it on, so a
// genuinely broken engine can still be skipped by its own score.
function isEngineOn(item: SearchEngineItem): boolean {
  return item.manual_state !== "forced_off";
}

async function setEngineManualState(
  engineId: string,
  state: EngineManualState,
): Promise<void> {
  if (engineBusy.value) return;
  engineBusy.value = engineId;
  try {
    await apiJson("POST", `/api/search/engines/${engineId}/manual_state`, {
      state,
    });
    await loadEngines();
  } catch (e) {
    toast.error(
      `${t("chat.search.advanced.loadFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    engineBusy.value = null;
  }
}

function onEngineToggle(item: SearchEngineItem): void {
  void setEngineManualState(
    item.engine_id,
    isEngineOn(item) ? "forced_off" : "auto",
  );
}

async function onEngineResetScore(engineId: string): Promise<void> {
  if (engineBusy.value) return;
  engineBusy.value = engineId;
  try {
    await apiJson<void>("DELETE", `/api/search/engines/${engineId}/score`);
    await loadEngines();
  } catch (e) {
    toast.error(
      `${t("chat.search.advanced.loadFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    engineBusy.value = null;
  }
}

function successRatePercent(rate: number): string {
  return `${Number.isFinite(rate) ? Math.round(rate * 100) : 0}%`;
}

// ─── API-key management (PUT/DELETE .../credential) ──────────────────────────
// The key value is never returned by the API; "configured" is derived purely
// from status.
const keyInputs = reactive<Record<string, string>>({});
const keyEditing = ref<string | null>(null);
const keyBusy = ref<string | null>(null);

function isEngineConfigured(item: SearchEngineItem): boolean {
  return item.requires_credential && item.status !== "needs_config";
}

function beginReplaceKey(engineId: string): void {
  keyInputs[engineId] = "";
  keyEditing.value = engineId;
}

function cancelReplaceKey(engineId: string): void {
  keyInputs[engineId] = "";
  if (keyEditing.value === engineId) keyEditing.value = null;
}

async function onEngineSaveKey(engineId: string): Promise<void> {
  if (keyBusy.value) return;
  const apiKey = (keyInputs[engineId] ?? "").trim();
  if (!apiKey) return;
  keyBusy.value = engineId;
  try {
    await apiJson("PUT", `/api/search/engines/${engineId}/credential`, {
      api_key: apiKey,
    });
    keyInputs[engineId] = "";
    if (keyEditing.value === engineId) keyEditing.value = null;
    await loadEngines();
  } catch (e) {
    toast.error(
      `${t("chat.search.credential.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    keyBusy.value = null;
  }
}

async function onEngineClearKey(engineId: string): Promise<void> {
  if (keyBusy.value) return;
  keyBusy.value = engineId;
  try {
    await apiJson<void>("DELETE", `/api/search/engines/${engineId}/credential`);
    keyInputs[engineId] = "";
    if (keyEditing.value === engineId) keyEditing.value = null;
    await loadEngines();
  } catch (e) {
    toast.error(
      `${t("chat.search.credential.clearFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    keyBusy.value = null;
  }
}
</script>

<template>
  <!-- `config-section` (shared settings.css) supplies the inter-group
       gap: var(--space-5) that every other settings panel relies on. -->
  <div class="config-section search-panel">
    <!-- ═══ Routing ═══════════════════════════════════════════════════════ -->
    <div class="config-group">
      <div class="config-group-header config-group-header--static">
        <span>🔎</span>
        <span>{{ t("chat.search.groupTitle") }}</span>
      </div>
      <div class="config-group-body">
        <div class="config-field">
          <label class="config-label">{{ t("chat.search.mode.label") }}</label>
          <div
            class="segmented-control"
            role="radiogroup"
            :aria-label="t('chat.search.mode.label')"
          >
            <label
              v-for="m in SEARCH_MODES"
              :key="m"
              class="segmented-control__option"
              :class="{ 'is-active': searchForm.mode === m }"
            >
              <input
                type="radio"
                name="search-mode"
                :value="m"
                :checked="searchForm.mode === m"
                :disabled="searchSaving"
                @change="onSearchModeChange(m)"
              />
              <span>{{
                m === "cebot_only"
                  ? t("chat.search.mode.cebotOnly")
                  : m === "web_only"
                    ? t("chat.search.mode.webOnly")
                    : t("chat.search.mode.both")
              }}</span>
            </label>
          </div>
        </div>

        <!-- Only meaningful when both sources are live. -->
        <div
          v-if="searchForm.mode === 'both'"
          class="config-field"
        >
          <label class="config-label">{{ t("chat.search.defaultProvider.label") }}</label>
          <div
            class="segmented-control"
            role="radiogroup"
            :aria-label="t('chat.search.defaultProvider.label')"
          >
            <label
              v-for="p in SEARCH_DEFAULT_PROVIDERS"
              :key="p"
              class="segmented-control__option"
              :class="{ 'is-active': searchForm.default_provider === p }"
            >
              <input
                type="radio"
                name="search-default-provider"
                :value="p"
                :checked="searchForm.default_provider === p"
                :disabled="searchSaving"
                @change="onSearchDefaultProviderChange(p)"
              />
              <span>{{
                p === "cebot"
                  ? t("chat.search.defaultProvider.cebot")
                  : p === "web"
                    ? t("chat.search.defaultProvider.web")
                    : t("chat.search.defaultProvider.auto")
              }}</span>
            </label>
          </div>
        </div>

        <p class="config-comment">{{ t("chat.search.description") }}</p>
      </div>
    </div>

    <!-- ═══ Load states ═══════════════════════════════════════════════════ -->
    <div
      v-if="enginesLoading"
      class="config-group"
    >
      <div class="config-group-body">
        <p class="config-comment">{{ t("chat.search.advanced.loading") }}</p>
      </div>
    </div>

    <div
      v-else-if="enginesError"
      class="config-group"
    >
      <div class="config-group-body">
        <p class="search-panel__error">{{ t("chat.search.advanced.loadFailed") }}</p>
        <button
          type="button"
          class="btn"
          @click="loadEngines"
        >
          {{ t("chat.search.advanced.retry") }}
        </button>
      </div>
    </div>

    <div
      v-else-if="engines.length === 0"
      class="config-group"
    >
      <div class="config-group-body">
        <p class="config-comment">{{ t("chat.search.advanced.empty") }}</p>
      </div>
    </div>

    <template v-else>
      <!-- ═══ Free engines ════════════════════════════════════════════════ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🆓</span>
          <span>{{ t("chat.search.groups.free.title") }}</span>
          <span class="search-panel__count">{{ freeEngines.length }}</span>
        </div>
        <div class="config-group-body">
          <p class="config-comment">{{ t("chat.search.groups.free.desc") }}</p>

          <ul class="search-engine-list">
            <li
              v-for="engine in freeEngines"
              :key="engine.engine_id"
              class="search-engine-card"
              :class="{ 'is-off': !isEngineOn(engine) }"
              :data-testid="`search-engine-${engine.engine_id}`"
            >
              <div class="search-engine-card__main">
                <div class="search-engine-card__title-row">
                  <span class="search-engine-card__name">{{ engine.display_name }}</span>
                  <span
                    v-if="qualityTier(engine)"
                    class="search-engine-card__tier"
                    :class="`search-engine-card__tier--${qualityTier(engine)}`"
                  >{{ qualityTierLabel(qualityTier(engine)!) }}</span>
                </div>
                <p class="search-engine-card__desc">{{ engineDescription(engine) }}</p>
                <div class="search-engine-card__meta">
                  <span>{{ t("chat.search.engineScore.successRate") }} {{ successRatePercent(engine.success_rate) }}</span>
                  <span class="search-engine-card__dot">·</span>
                  <span>{{ t("chat.search.engineScore.scoreLabel") }} {{ engine.score }}</span>
                  <button
                    type="button"
                    class="search-engine-card__reset"
                    :disabled="engineBusy === engine.engine_id"
                    @click="onEngineResetScore(engine.engine_id)"
                  >
                    {{ t("chat.search.engineScore.reset") }}
                  </button>
                </div>
              </div>

              <!-- One switch per engine: the only control most users need. -->
              <button
                type="button"
                class="search-switch"
                :class="{ 'is-on': isEngineOn(engine) }"
                role="switch"
                :aria-checked="isEngineOn(engine)"
                :aria-label="t('chat.search.toggle.enableAria', { name: engine.display_name })"
                :disabled="engineBusy === engine.engine_id"
                :data-testid="`search-toggle-${engine.engine_id}`"
                @click="onEngineToggle(engine)"
              >
                <span class="search-switch__track"><span class="search-switch__thumb" /></span>
                <span class="search-switch__label">{{
                  isEngineOn(engine) ? t("chat.search.toggle.on") : t("chat.search.toggle.off")
                }}</span>
              </button>
            </li>
          </ul>

          <details class="search-panel__scoring">
            <summary class="search-panel__scoring-summary">
              {{ t("chat.search.quality.measured") }}
              <span class="search-panel__scoring-hint">{{ t("chat.search.scoring.title") }}</span>
            </summary>
            <div class="search-panel__scoring-body">
              <p>{{ t("chat.search.scoring.intro") }}</p>
              <ul>
                <li>{{ t("chat.search.scoring.ruleSuccess") }}</li>
                <li>{{ t("chat.search.scoring.ruleDedup") }}</li>
                <li>{{ t("chat.search.scoring.ruleDailyCap") }}</li>
                <li>{{ t("chat.search.scoring.ruleHardFail") }}</li>
                <li>{{ t("chat.search.scoring.ruleOrder") }}</li>
                <li>{{ t("chat.search.scoring.ruleDisable") }}</li>
              </ul>
              <p>{{ t("chat.search.scoring.manualHint") }}</p>
            </div>
          </details>
        </div>
      </div>

      <!-- ═══ API-key engines ═════════════════════════════════════════════ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🔑</span>
          <span>{{ t("chat.search.groups.paid.title") }}</span>
          <span class="search-panel__count">{{ paidEngines.length }}</span>
        </div>
        <div class="config-group-body">
          <p class="config-comment">{{ t("chat.search.groups.paid.desc") }}</p>

          <ul class="search-engine-list">
            <li
              v-for="engine in visiblePaidEngines"
              :key="engine.engine_id"
              class="search-engine-card"
              :class="{ 'is-unconfigured': engine.status === 'needs_config' }"
              :data-testid="`search-engine-${engine.engine_id}`"
            >
              <div class="search-engine-card__main">
                <div class="search-engine-card__title-row">
                  <span class="search-engine-card__name">{{ engine.display_name }}</span>
                  <span
                    v-if="engine.status === 'needs_config'"
                    class="search-engine-card__badge search-engine-card__badge--needs"
                  >{{ t("chat.search.engineScore.statusNeedsConfig") }}</span>
                  <span
                    v-else
                    class="search-engine-card__badge search-engine-card__badge--ok"
                  >{{ t("chat.search.credential.configured") }}</span>
                </div>
                <p class="search-engine-card__desc">{{ engineDescription(engine) }}</p>

                <div
                  v-if="isEngineConfigured(engine)"
                  class="search-engine-card__meta"
                >
                  <span>{{ t("chat.search.engineScore.successRate") }} {{ successRatePercent(engine.success_rate) }}</span>
                  <span
                    v-if="engine.quota_limit"
                    class="search-engine-card__dot"
                  >·</span>
                  <span v-if="engine.quota_limit">
                    {{ engine.quota_usage ?? 0 }} / {{ engine.quota_limit }}
                  </span>
                </div>

                <!-- Key input: shown when unconfigured, or while replacing. -->
                <div
                  v-if="engine.status === 'needs_config' || keyEditing === engine.engine_id"
                  class="search-key-row"
                >
                  <input
                    :id="`engine-key-${engine.engine_id}`"
                    v-model="keyInputs[engine.engine_id]"
                    type="password"
                    class="config-input search-key-input"
                    autocomplete="off"
                    :placeholder="t('chat.search.credential.apiKeyPlaceholder')"
                    :disabled="keyBusy === engine.engine_id"
                    @keyup.enter="onEngineSaveKey(engine.engine_id)"
                  />
                  <button
                    type="button"
                    class="btn btn--primary"
                    :disabled="keyBusy === engine.engine_id || !(keyInputs[engine.engine_id] ?? '').trim()"
                    @click="onEngineSaveKey(engine.engine_id)"
                  >
                    {{ t("chat.search.credential.save") }}
                  </button>
                  <button
                    v-if="keyEditing === engine.engine_id"
                    type="button"
                    class="btn"
                    :disabled="keyBusy === engine.engine_id"
                    @click="cancelReplaceKey(engine.engine_id)"
                  >
                    {{ t("chat.search.credential.cancel") }}
                  </button>
                </div>
                <div
                  v-else-if="isEngineConfigured(engine)"
                  class="search-key-row"
                >
                  <button
                    type="button"
                    class="btn"
                    :disabled="keyBusy === engine.engine_id"
                    @click="beginReplaceKey(engine.engine_id)"
                  >
                    {{ t("chat.search.credential.replace") }}
                  </button>
                  <button
                    type="button"
                    class="btn"
                    :disabled="keyBusy === engine.engine_id"
                    @click="onEngineClearKey(engine.engine_id)"
                  >
                    {{ t("chat.search.credential.clear") }}
                  </button>
                </div>
              </div>

              <!-- A keyed engine without a key has nothing to switch. -->
              <button
                v-if="isEngineConfigured(engine)"
                type="button"
                class="search-switch"
                :class="{ 'is-on': isEngineOn(engine) }"
                role="switch"
                :aria-checked="isEngineOn(engine)"
                :aria-label="t('chat.search.toggle.enableAria', { name: engine.display_name })"
                :disabled="engineBusy === engine.engine_id"
                :data-testid="`search-toggle-${engine.engine_id}`"
                @click="onEngineToggle(engine)"
              >
                <span class="search-switch__track"><span class="search-switch__thumb" /></span>
                <span class="search-switch__label">{{
                  isEngineOn(engine) ? t("chat.search.toggle.on") : t("chat.search.toggle.off")
                }}</span>
              </button>
            </li>
          </ul>

          <!-- Collapse toggle, mirroring the model list's "show N more". -->
          <button
            v-if="paidHiddenCount > 0"
            type="button"
            class="search-panel__more"
            data-testid="search-paid-show-more"
            @click="paidExpanded = true"
          >
            {{ t("chat.search.showMore", { n: paidHiddenCount }) }}
          </button>
          <button
            v-else-if="paidExpanded && paidUnconfigured.length > 0"
            type="button"
            class="search-panel__more"
            data-testid="search-paid-show-less"
            @click="paidExpanded = false"
          >
            ▲ {{ t("chat.search.showLess") }}
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* Reuses the shared settings design language (`frontend/src/styles/common/settings.css`):
   `config-section` for the inter-group gap, `config-group` / `config-group-header`
   / `config-group-body` for the card chrome, `config-comment` / `config-input`
   for text and fields. Everything below is only what those classes do not
   already cover — the per-engine row and its switch.

   THEME TOKENS ONLY. An earlier revision hard-coded hex colours
   (#e8eaed / #9aa0a6 / rgba(255,255,255,…)), which read as off-palette in dark
   mode and became unreadable in light mode: variables.css re-defines the whole
   surface/text ramp under the light theme, so a literal colour cannot follow it. */

/* Count chip on the group header, right-aligned like the shared collapse arrow. */
.search-panel__count {
  margin-left: auto;
  padding: 1px var(--space-2);
  border-radius: var(--radius-full);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.search-panel__error {
  margin: 0;
  color: var(--error);
  font-size: var(--text-sm);
}

/* ── Scoring explainer ───────────────────────────────────────────────────
   A `<details>` so the rules are one click away without pushing the engine
   list off-screen. The closed summary keeps the original "ordered by measured
   quality" note, so nothing is lost when it stays collapsed. */
.search-panel__scoring {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.search-panel__scoring-summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  list-style: none;
}

.search-panel__scoring-summary::-webkit-details-marker {
  display: none;
}

.search-panel__scoring-summary::before {
  content: "▸";
  color: var(--text-muted);
  transition: transform 120ms ease;
}

.search-panel__scoring[open] .search-panel__scoring-summary::before {
  transform: rotate(90deg);
}

.search-panel__scoring-hint {
  color: var(--accent);
}

.search-panel__scoring-body {
  margin-top: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
  line-height: 1.6;
}

.search-panel__scoring-body p {
  margin: 0;
}

.search-panel__scoring-body ul {
  margin: var(--space-2) 0;
  padding-left: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

/* ── Engine list ─────────────────────────────────────────────────────────
   `config-group-body` is already a flex column with gap: var(--space-4);
   the list sets a tighter gap so a long roster stays scannable without the
   rows blurring together. */
.search-engine-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.search-engine-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
  transition: border-color var(--duration-fast) var(--ease-out),
              background var(--duration-fast) var(--ease-out);
}

.search-engine-card:hover {
  border-color: var(--accent);
  background: var(--bg-hover);
}

/* A switched-off engine stays readable but visibly inert: the user must be able
   to tell at a glance which sources are actually being queried. Dimming the
   TEXT rather than the whole card keeps the switch at full contrast so it can
   still be found and clicked. */
.search-engine-card.is-off .search-engine-card__name,
.search-engine-card.is-off .search-engine-card__desc,
.search-engine-card.is-off .search-engine-card__meta {
  opacity: 0.45;
}

/* An unconfigured keyed engine is a call to action, not a failure state. */
.search-engine-card.is-unconfigured {
  border-style: dashed;
}

.search-engine-card__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.search-engine-card__title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.search-engine-card__name {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text-primary);
}

/* Quality tier / status badges share one shape so the eye reads them as one
   kind of annotation; only the colour differs. */
.search-engine-card__tier,
.search-engine-card__badge {
  padding: 1px var(--space-2);
  border-radius: var(--radius-xs);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.5;
  white-space: nowrap;
}

/* Uses the established --banner-{success,warn}-{bg,text} family rather than a
   local color-mix(): those tokens are already tuned per theme (the light theme
   darkens the text so it stays legible on a pale surface), and they avoid
   color-mix(), which resolves to `color(srgb …)` — newer syntax than anything
   else in this sheet relies on. */
.search-engine-card__tier--high,
.search-engine-card__badge--ok {
  background: var(--banner-success-bg);
  color: var(--banner-success-text);
}
.search-engine-card__tier--medium,
.search-engine-card__badge--needs {
  background: var(--banner-warn-bg);
  color: var(--banner-warn-text);
}
.search-engine-card__tier--low {
  background: var(--bg-secondary);
  color: var(--text-muted);
}

.search-engine-card__desc {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.5;
  color: var(--text-secondary);
}

/* Score / success rate are diagnostics, not primary information: one quiet
   line, and the reset action reads as a link rather than a button. */
.search-engine-card__meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.search-engine-card__dot {
  opacity: 0.5;
}

.search-engine-card__reset {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: var(--text-xs);
  font-family: inherit;
  cursor: pointer;
}
.search-engine-card__reset:hover:not(:disabled) {
  text-decoration: underline;
}
.search-engine-card__reset:disabled {
  opacity: 0.5;
  cursor: default;
}

/* ── Switch ──────────────────────────────────────────────────────────────
   Replaces the previous three-radio group (auto / forced_on / forced_off),
   which exposed the backend's manual_state verbatim and asked the user to
   reason about scheduling policy. What they want is "use this engine or
   don't". ON maps to `auto` (health-score scheduling), never `forced_on`, so a
   genuinely failing engine can still be skipped by its own score. */
.search-switch {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
}
.search-switch:disabled {
  opacity: 0.5;
  cursor: default;
}

.search-switch__track {
  position: relative;
  display: block;
  width: 34px;
  height: 18px;
  border-radius: var(--radius-full);
  background: var(--border);
  transition: background var(--duration-fast) var(--ease-out);
}

.search-switch__thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: var(--radius-full);
  background: var(--bg-primary);
  box-shadow: 0 1px 2px rgb(0 0 0 / 25%);
  transition: transform var(--duration-fast) var(--ease-out);
}

.search-switch.is-on .search-switch__track {
  background: var(--accent);
}
.search-switch.is-on .search-switch__thumb {
  transform: translateX(16px);
}

.search-switch__label {
  min-width: 3.4em;
  font-size: var(--text-xs);
  color: var(--text-muted);
  text-align: left;
}
.search-switch.is-on .search-switch__label {
  color: var(--text-primary);
}

.search-switch:focus-visible .search-switch__track {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* ── Key row ─────────────────────────────────────────────────────────── */
.search-key-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.search-key-input {
  flex: 1 1 220px;
  min-width: 160px;
  max-width: 320px;
}

/* ── Collapse toggle (mirrors the model list's "show N more") ─────────── */
.search-panel__more {
  width: 100%;
  padding: var(--space-2);
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-family: inherit;
  cursor: pointer;
  transition: border-color var(--duration-fast) var(--ease-out),
              color var(--duration-fast) var(--ease-out);
}
.search-panel__more:hover {
  border-color: var(--accent);
  color: var(--text-primary);
}

/* Narrow viewports: the switch drops below the text instead of squeezing it. */
@media (max-width: 560px) {
  .search-engine-card {
    flex-direction: column;
  }
  .search-switch {
    align-self: flex-start;
  }
}
</style>
