<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import ActiveRunBar from "@/components/ActiveRunBar.vue";
import Composer from "@/components/Composer.vue";
import DocsView from "@/components/DocsView.vue";
import InspectorDrawer from "@/components/InspectorDrawer.vue";
import RunTimeline from "@/components/RunTimeline.vue";
import SessionSidebar from "@/components/SessionSidebar.vue";
import SettingsDialog from "@/components/SettingsDialog.vue";
import ToastHost from "@/components/ToastHost.vue";
import UmoDialog from "@/components/UmoDialog.vue";

import { hasBridge, ready } from "@/api/bridge";
import { useConsoleSync } from "@/composables/useConsoleSync";
import { toast, toastError } from "@/composables/useToast";
import {
  activeRunView,
  agentNames,
  deleteAgent,
  exportTranscript,
  forkRun,
  loadRunTrace,
  mistressName,
  readRunResult,
  refresh,
  rewindToRun,
  runViews,
  saveSettings,
  selectAgent,
  selectUmo,
  selectedAgent,
  state,
  stopRun,
  submitPrompt,
  visibleSessions,
} from "@/store/console";
import { displayAgentTitle } from "@/utils/alias";

const SIDEBAR_KEY = "maid_console_sidebar_collapsed";

const sync = useConsoleSync();

const timeline = ref(null);
const composer = ref(null);
const sidebar = ref(null);

const sidebarCollapsed = ref(false);
const mobileSidebarOpen = ref(false);
const drawerOpen = ref(false);
const inspectTaskId = ref("");
const settingsOpen = ref(false);
const umoOpen = ref(false);
const booting = ref(true);

const headerTitle = computed(() => {
  if (state.view === "docs") return "使用文档";
  return displayAgentTitle(selectedAgent.value) || "新任务";
});

const composerMode = computed(() => {
  if (!selectedAgent.value) return "dispatch";
  return selectedAgent.value.active_task_id ? "steer" : "resume";
});

const inspectView = computed(() => {
  if (!inspectTaskId.value) return activeRunView.value || runViews.value.at(-1) || null;
  return runViews.value.find((view) => view.taskId === inspectTaskId.value) || null;
});

const inspectTrace = computed(() =>
  inspectView.value ? state.runTrace[inspectView.value.taskId] || null : null,
);

/* -------------------------------------------------------------- 动作 */

async function onNewChat() {
  mobileSidebarOpen.value = false;
  await selectAgent("");
  await nextTick();
  composer.value?.focus();
}

async function onSelectSession(agentId) {
  mobileSidebarOpen.value = false;
  inspectTaskId.value = "";
  await selectAgent(agentId);
  await nextTick();
  composer.value?.focus();
}

function onToggleDocs() {
  state.view = state.view === "docs" ? "timeline" : "docs";
}

async function onSubmit({ text, imagePaths, done }) {
  try {
    const agentId = await submitPrompt({ text, imagePaths });
    done(true);
    if (agentId && agentId !== state.selectedAgentId) {
      await selectAgent(agentId);
    }
    await nextTick();
    timeline.value?.scrollToBottom({ smooth: false });
    composer.value?.focus();
  } catch (err) {
    done(false);
    if (String(err?.message || "").includes("UMO")) {
      umoOpen.value = true;
    }
    toastError(err, "发送失败");
  }
}

function onInspect(view) {
  inspectTaskId.value = view.taskId;
  drawerOpen.value = true;
}

async function onFork(view) {
  const agentId = await forkRun(view.taskId);
  if (agentId) {
    await nextTick();
    timeline.value?.scrollToBottom({ smooth: false });
  }
}

async function onRewind(view) {
  await rewindToRun(state.selectedAgentId, view.taskId);
}

function onSelectUmo(umo) {
  selectUmo(umo);
  umoOpen.value = false;
}

async function onSaveSettings(payload) {
  try {
    await saveSettings(payload);
    settingsOpen.value = false;
    toast("配置已保存");
  } catch (err) {
    toastError(err, "保存失败");
  }
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  try {
    localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed.value ? "1" : "");
  } catch {
    /* sandboxed iframe 里没有 storage：只在本次会话生效 */
  }
}

/* -------------------------------------------------------------- 快捷键 */

function isEditing(target) {
  const el = target instanceof HTMLElement ? target : null;
  if (!el) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

function onKeydown(event) {
  if (event.key === "Escape") {
    sidebar.value?.closeMenu();
    if (settingsOpen.value) settingsOpen.value = false;
    else if (umoOpen.value) umoOpen.value = false;
    else if (drawerOpen.value) drawerOpen.value = false;
    else if (mobileSidebarOpen.value) mobileSidebarOpen.value = false;
    return;
  }
  if (event.key === "/" && !isEditing(event.target)) {
    event.preventDefault();
    composer.value?.focus();
  }
}

/* -------------------------------------------------------------- 启动 */

onMounted(async () => {
  document.addEventListener("keydown", onKeydown);
  try {
    sidebarCollapsed.value = localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    /* 同上 */
  }

  if (!hasBridge()) {
    state.streamState = "error";
    booting.value = false;
    toast("页面桥接未加载，请从 AstrBot 插件页重新打开控制台", "error");
    return;
  }

  try {
    await ready();
    await refresh({ silent: true, keepSession: false });
    await sync.start();
  } catch (err) {
    state.streamState = "error";
    toastError(err, "启动失败");
  } finally {
    booting.value = false;
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <div class="shell">
    <SessionSidebar
      ref="sidebar"
      class="shell-sidebar"
      :class="{ 'is-mobile-open': mobileSidebarOpen }"
      :sessions="visibleSessions"
      :selected-id="state.selectedAgentId"
      :collapsed="sidebarCollapsed"
      :filter="state.sessionFilter"
      :umo="state.selectedUmo"
      :stream-state="state.streamState"
      :last-sync-at="state.lastSyncAt"
      :docs-mode="state.view === 'docs'"
      @update:filter="state.sessionFilter = $event"
      @select="onSelectSession"
      @new-chat="onNewChat"
      @toggle-docs="onToggleDocs"
      @toggle-collapse="toggleSidebar"
      @open-umo="umoOpen = true"
      @open-settings="settingsOpen = true"
      @export="exportTranscript"
      @delete="deleteAgent"
    />
    <div
      v-if="mobileSidebarOpen"
      class="sidebar-scrim"
      @click="mobileSidebarOpen = false"
    />

    <main class="shell-main">
      <header class="top-bar">
        <button
          class="icon-btn mobile-only"
          type="button"
          aria-label="打开侧栏"
          @click="mobileSidebarOpen = true"
        >
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>
        <h1 class="top-title">{{ headerTitle }}</h1>
        <span class="top-spacer" />
        <button
          class="icon-btn"
          type="button"
          aria-label="立即刷新"
          title="立即刷新"
          :disabled="state.refreshInFlight"
          @click="refresh({ silent: false, keepSession: true })"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 0 1-15.5 6.3L3 16" /><path d="M3 21v-5h5" />
            <path d="M3 12a9 9 0 0 1 15.5-6.3L21 8" /><path d="M21 3v5h-5" />
          </svg>
        </button>
        <button
          class="icon-btn"
          type="button"
          aria-label="调试面板"
          title="调试面板"
          :disabled="!selectedAgent"
          @click="drawerOpen = !drawerOpen"
        >
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" /><line x1="15" y1="3" x2="15" y2="21" />
          </svg>
        </button>
      </header>

      <ActiveRunBar
        v-if="state.view === 'timeline' && activeRunView"
        :view="activeRunView"
        :title="displayAgentTitle(selectedAgent)"
        @stop="stopRun(activeRunView.taskId)"
        @jump="timeline?.scrollToBottom({ smooth: true })"
      />

      <DocsView v-if="state.view === 'docs'" />

      <template v-else>
        <div v-if="booting" class="empty-state">正在连接 AstrBot…</div>
        <div v-else-if="!state.selectedAgentId" class="hero">
          <p class="hero-title">派一个新任务</p>
          <p class="hero-sub">
            输入要求后发送，插件会新建一个管家 Agent 并在后台执行。左侧可以切回已有会话。
          </p>
        </div>
        <RunTimeline
          v-else
          ref="timeline"
          :views="runViews"
          :mistress-name="mistressName"
          :agent-id="state.selectedAgentId"
          @stop="stopRun($event.taskId)"
          @rewind="onRewind"
          @fork="onFork"
          @result="readRunResult(state.selectedAgentId, $event.taskId)"
          @inspect="onInspect"
        />

        <div class="composer-wrap">
          <Composer
            ref="composer"
            :agent-names="agentNames"
            :dispatch-agent="state.dispatchAgent"
            :mode="composerMode"
            @update:dispatch-agent="state.dispatchAgent = $event"
            @submit="onSubmit"
          />
        </div>
      </template>
    </main>

    <InspectorDrawer
      :open="drawerOpen"
      :view="inspectView"
      :agent="selectedAgent"
      :trace="inspectTrace"
      @close="drawerOpen = false"
      @need-trace="loadRunTrace"
      @export="exportTranscript"
    />

    <SettingsDialog
      :open="settingsOpen"
      :config="state.settings"
      @close="settingsOpen = false"
      @save="onSaveSettings"
    />

    <UmoDialog
      :open="umoOpen"
      :umos="state.umos"
      :selected="state.selectedUmo"
      @close="umoOpen = false"
      @select="onSelectUmo"
    />

    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100%;
  width: 100%;
}
.shell-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-app);
}

.top-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 46px;
  padding: 0 10px 0 16px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--line);
}
.top-title {
  margin: 0;
  font-size: 13.5px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 46vw;
}
.top-spacer {
  flex: 1;
}

.hero {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  padding: 0 24px 18px;
  text-align: center;
}
.hero-title {
  margin: 0 0 6px;
  font-size: 19px;
  font-weight: 600;
}
.hero-sub {
  margin: 0;
  max-width: 34rem;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-muted);
}

.composer-wrap {
  flex-shrink: 0;
  width: 100%;
  max-width: calc(var(--content-width) + 40px);
  margin: 0 auto;
  padding: 8px 20px 16px;
}

.sidebar-scrim {
  display: none;
}
.mobile-only {
  display: none;
}

@media (max-width: 860px) {
  .shell-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 400;
    transform: translateX(-100%);
    transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: var(--shadow-modal);
  }
  .shell-sidebar.is-mobile-open {
    transform: translateX(0);
  }
  .sidebar-scrim {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(20, 22, 24, 0.45);
    z-index: 350;
  }
  .mobile-only {
    display: inline-flex;
  }
  .composer-wrap {
    padding: 8px 12px 12px;
  }
  .top-title {
    max-width: 38vw;
  }
}
</style>
