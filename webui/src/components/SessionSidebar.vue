<script setup>
import { computed, nextTick, ref, watch } from "vue";

import { formatClock, formatTime } from "@/utils/format";
import { displayAgentTitle } from "@/utils/alias";
import { useTimedConfirm } from "@/composables/useTimedConfirm";

const props = defineProps({
  sessions: { type: Array, default: () => [] },
  selectedId: { type: String, default: "" },
  collapsed: { type: Boolean, default: false },
  filter: { type: String, default: "" },
  umo: { type: String, default: "" },
  streamState: { type: String, default: "idle" },
  lastSyncAt: { type: String, default: "" },
  docsMode: { type: Boolean, default: false },
});

const emit = defineEmits([
  "update:filter",
  "select",
  "new-chat",
  "toggle-docs",
  "toggle-collapse",
  "open-umo",
  "open-settings",
  "export",
  "delete",
]);

const listEl = ref(null);
const focusIndex = ref(0);

const menuFor = ref("");
const menuPos = ref({ top: 0, left: 0 });

/** 删除确认。菜单关掉或切走时 disarm，避免下次打开仍处于 armed 态。 */
const { armed: deleteArmed, trigger: deleteTrigger, disarm: deleteDisarm } =
  useTimedConfirm();

const STREAM_LABEL = {
  idle: "连接中",
  live: "实时同步",
  poll: "轮询同步",
  error: "同步异常",
};

const streamLabel = computed(() => STREAM_LABEL[props.streamState] || "未知状态");

watch(
  () => props.selectedId,
  (id) => {
    const index = props.sessions.findIndex((item) => item.agent_id === id);
    if (index >= 0) focusIndex.value = index;
  },
);

function subtitle(agent) {
  return [
    agent.agent_name,
    agent.last_status || "unknown",
    agent.run_count > 1 ? `${agent.run_count} runs` : `${agent.run_count || 0} run`,
    formatTime(agent.last_run_at || agent.updated_at),
  ]
    .filter(Boolean)
    .join(" · ");
}

/** 方向键在列表内移动焦点（roving tabindex），Tab 只进出列表一次。 */
async function onListKeydown(event) {
  const total = props.sessions.length;
  if (!total) return;
  let next = focusIndex.value;
  if (event.key === "ArrowDown") next = Math.min(total - 1, focusIndex.value + 1);
  else if (event.key === "ArrowUp") next = Math.max(0, focusIndex.value - 1);
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = total - 1;
  else return;

  event.preventDefault();
  focusIndex.value = next;
  await nextTick();
  listEl.value?.querySelectorAll(".session-item")[next]?.focus();
}

function openMenu(event, agentId) {
  const rect = event.currentTarget.getBoundingClientRect();
  if (menuFor.value === agentId) {
    closeMenu();
    return;
  }
  menuFor.value = agentId;
  deleteDisarm();
  menuPos.value = {
    top: Math.min(rect.bottom + 4, window.innerHeight - 120),
    left: Math.min(rect.left, window.innerWidth - 190),
  };
}

function closeMenu() {
  menuFor.value = "";
  deleteDisarm();
}

const menuAgent = computed(
  () => props.sessions.find((item) => item.agent_id === menuFor.value) || null,
);

defineExpose({ closeMenu });
</script>

<template>
  <aside class="sidebar" :class="{ 'is-collapsed': collapsed }">
    <div class="sidebar-top">
      <span class="brand sidebar-label">代理女仆 Console</span>
      <button
        class="icon-btn"
        type="button"
        :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'"
        :title="collapsed ? '展开侧栏' : '折叠侧栏'"
        @click="emit('toggle-collapse')"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="9" y1="3" x2="9" y2="21" />
        </svg>
      </button>
    </div>

    <div class="sidebar-actions">
      <button class="wide-btn" type="button" @click="emit('new-chat')">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        <span class="sidebar-label">新任务</span>
      </button>
      <button
        class="wide-btn"
        :class="{ 'is-active': docsMode }"
        type="button"
        @click="emit('toggle-docs')"
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
        </svg>
        <span class="sidebar-label">使用文档</span>
      </button>
    </div>

    <div class="sidebar-filter sidebar-label">
      <input
        :value="filter"
        type="search"
        placeholder="筛选会话…"
        aria-label="筛选会话"
        autocomplete="off"
        @input="emit('update:filter', $event.target.value)"
      />
    </div>

    <div
      ref="listEl"
      class="session-list sidebar-label"
      role="listbox"
      aria-label="Agent 会话"
      @keydown="onListKeydown"
    >
      <p v-if="!sessions.length" class="empty-state">暂无 Agent 会话</p>

      <div
        v-for="(agent, index) in sessions"
        :key="agent.agent_id"
        class="session-row"
        :class="{ 'is-selected': agent.agent_id === selectedId }"
      >
        <button
          class="session-item"
          type="button"
          role="option"
          :aria-selected="agent.agent_id === selectedId"
          :tabindex="index === focusIndex ? 0 : -1"
          @click="emit('select', agent.agent_id)"
          @focus="focusIndex = index"
        >
          <span class="session-title">{{ displayAgentTitle(agent) }}</span>
          <span class="session-sub">
            <span class="status-dot" :class="`is-${agent.last_status || 'unknown'}`" />
            {{ subtitle(agent) }}<template v-if="agent.pending_notification"> · 待通知</template>
          </span>
        </button>
        <button
          class="session-more icon-btn"
          type="button"
          aria-label="更多操作"
          title="更多操作"
          @click.stop="openMenu($event, agent.agent_id)"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" />
          </svg>
        </button>
      </div>
    </div>

    <div class="sidebar-footer">
      <div class="sync-line sidebar-label">
        <span class="signal" :class="`is-${streamState}`">{{ streamLabel }}</span>
        <span v-if="lastSyncAt" class="sync-time">{{ formatClock(lastSyncAt) }}</span>
      </div>
      <div class="footer-row">
        <button class="umo-switch" type="button" @click="emit('open-umo')">
          <span class="umo-avatar" aria-hidden="true">{{ (umo || "?").slice(0, 2).toUpperCase() }}</span>
          <span class="umo-name sidebar-label">{{ umo || "选择 UMO" }}</span>
        </button>
        <button
          class="icon-btn"
          type="button"
          aria-label="全局配置"
          title="全局配置"
          @click="emit('open-settings')"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="menuAgent" class="menu-scrim" @click="closeMenu" />
      <div
        v-if="menuAgent"
        class="menu-popover"
        role="menu"
        :style="{ top: `${menuPos.top}px`, left: `${menuPos.left}px` }"
      >
        <button
          class="menu-item"
          type="button"
          @click="
            emit('export', menuAgent.agent_id);
            closeMenu();
          "
        >
          导出对话
        </button>
        <button
          class="menu-item is-danger"
          :class="{ 'is-armed': deleteArmed }"
          type="button"
          :disabled="Boolean(menuAgent.active_task_id) || menuAgent.pending_notification"
          :title="
            menuAgent.active_task_id
              ? 'Agent 仍在运行，请先停止'
              : menuAgent.pending_notification
                ? '请先读取待通知结果'
                : ''
          "
          @click="
            if (!(menuAgent.active_task_id || menuAgent.pending_notification)) {
              if (deleteTrigger()) {
                emit('delete', menuAgent.agent_id);
                closeMenu();
              }
            }
          "
        >
          {{ deleteArmed ? "再次点击确认删除" : "删除 Agent…" }}
        </button>
      </div>
    </Teleport>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--left-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--line);
  overflow: hidden;
  transition: width 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
.sidebar.is-collapsed {
  width: var(--left-rail);
}
.sidebar.is-collapsed .sidebar-label {
  display: none;
}
.sidebar.is-collapsed .sidebar-top,
.sidebar.is-collapsed .footer-row {
  flex-direction: column;
  gap: 6px;
}
.sidebar.is-collapsed .wide-btn {
  justify-content: center;
}

.sidebar-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 10px 4px 14px;
  min-height: 44px;
}
.brand {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidebar-actions {
  padding: 2px 8px 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.wide-btn {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 10px;
  border-radius: var(--radius-item);
  font-size: 13.5px;
  font-weight: 500;
  color: var(--accent);
}
.wide-btn:hover,
.wide-btn.is-active {
  background: var(--accent-soft);
}
.wide-btn svg {
  flex-shrink: 0;
}

.sidebar-filter {
  padding: 2px 8px 6px;
}
.sidebar-filter input {
  width: 100%;
  padding: 6px 9px;
  border-radius: var(--radius-item);
  border: 1px solid var(--line);
  background: var(--bg-app);
  font-size: 12.5px;
}
.sidebar-filter input:focus {
  border-color: var(--accent);
}

.session-list {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0 8px 8px;
}
.session-row {
  position: relative;
  display: flex;
  align-items: stretch;
  border-radius: var(--radius-item);
}
.session-row:hover {
  background: var(--bg-app);
}
.session-row.is-selected {
  background: var(--bg-app);
  box-shadow: inset 2px 0 0 var(--accent);
}
.session-item {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
  text-align: left;
  padding: 6px 4px 6px 9px;
  border-radius: var(--radius-item);
}
.session-title {
  font-size: 13px;
  color: var(--text-main);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-row.is-selected .session-title {
  font-weight: 600;
}
.session-sub {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: var(--size-meta);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-more {
  align-self: center;
  visibility: hidden;
  flex-shrink: 0;
}
.session-row:hover .session-more,
.session-row.is-selected .session-more,
.session-item:focus-visible ~ .session-more {
  visibility: visible;
}

.sidebar-footer {
  border-top: 1px solid var(--line);
  padding: 6px 8px 10px;
}
.sync-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 6px 5px;
  font-size: var(--size-meta);
}
.signal {
  display: inline-flex;
  align-items: center;
  color: var(--text-muted);
}
.signal::before {
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  background: currentColor;
}
.signal.is-live {
  color: var(--ok);
}
.signal.is-error {
  color: var(--err);
}
.sync-time {
  font-family: var(--font-mono);
  color: var(--text-dim);
}

.footer-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.umo-switch {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: var(--radius-item);
}
.umo-switch:hover {
  background: var(--bg-app);
}
.umo-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--line-strong);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.umo-name {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>

<style>
/* Teleport 到 body 的菜单，不能用 scoped */
.menu-scrim {
  position: fixed;
  inset: 0;
  z-index: 490;
}
.menu-popover {
  position: fixed;
  z-index: 500;
  min-width: 180px;
  max-width: 240px;
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow-modal);
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.menu-popover .menu-item {
  display: flex;
  align-items: center;
  padding: 7px 9px;
  border-radius: 5px;
  font-size: 13px;
  color: var(--text-main);
  text-align: left;
  width: 100%;
}
.menu-popover .menu-item:hover:not(:disabled) {
  background: var(--bg-sunken);
}
.menu-popover .menu-item.is-danger {
  color: var(--err);
}
.menu-popover .menu-item.is-armed {
  background: var(--err-soft);
  color: var(--err);
}
</style>
