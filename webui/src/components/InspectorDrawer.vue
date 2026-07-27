<script setup>
import { computed, ref, watch } from "vue";

import { copyText } from "@/composables/useCopy";
import { displayAgentTitle } from "@/utils/alias";
import { compactId, formatTime } from "@/utils/format";

const props = defineProps({
  open: { type: Boolean, default: false },
  view: { type: Object, default: null },
  agent: { type: Object, default: null },
  trace: { type: Object, default: null },
});

const emit = defineEmits(["close", "need-trace", "export"]);

const tab = ref("facts");

// 只有真正切到「原始 messages」时才去拉 trace，也只有那时才序列化。
watch(
  [() => props.open, tab, () => props.view?.taskId],
  ([open, current, taskId]) => {
    if (open && current === "raw" && taskId) emit("need-trace", taskId);
  },
  { immediate: true },
);

const facts = computed(() => {
  const view = props.view;
  const agent = props.agent;
  if (!agent) return [];
  if (!view) {
    return [
      ["Agent", displayAgentTitle(agent)],
      ["Agent ID", agent.agent_id],
      ["来源 UMO", agent.unified_msg_origin],
      ["Run 数", String(agent.run_count ?? 0)],
      ["最近活动", formatTime(agent.last_run_at || agent.updated_at)],
    ];
  }
  const run = view.run || {};
  return [
    ["Task ID", view.taskId],
    ["状态", view.status],
    ["Agent", displayAgentTitle(agent)],
    ["模式", run.mode || "-"],
    ["通知", run.notification?.notification_id || "-"],
    ["来源 UMO", run.unified_msg_origin || agent.unified_msg_origin],
    ["创建", formatTime(run.created_at)],
    [run.ended_at ? "完成" : "更新", formatTime(run.ended_at || run.updated_at)],
  ];
});

const rawMessages = computed(() => {
  const messages = props.trace?.messages;
  if (!Array.isArray(messages) || !messages.length) return "";
  return JSON.stringify(messages, null, 2);
});
</script>

<template>
  <aside class="drawer" :class="{ 'is-open': open }" :aria-hidden="String(!open)">
    <header class="drawer-head">
      <div class="drawer-title">
        <p class="eyebrow">调试</p>
        <h2>{{ view ? compactId(view.taskId) : displayAgentTitle(agent) || "未选择" }}</h2>
      </div>
      <button class="icon-btn" type="button" aria-label="关闭详情" @click="emit('close')">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </header>

    <div class="tab-row" role="tablist">
      <button
        v-for="item in [
          { id: 'facts', label: '属性' },
          { id: 'text', label: '原文' },
          { id: 'raw', label: '原始 messages' },
        ]"
        :key="item.id"
        class="tab"
        :class="{ 'is-active': tab === item.id }"
        type="button"
        role="tab"
        :aria-selected="tab === item.id"
        @click="tab = item.id"
      >
        {{ item.label }}
      </button>
    </div>

    <div class="drawer-body">
      <template v-if="tab === 'facts'">
        <dl v-if="facts.length" class="facts">
          <template v-for="[key, value] in facts" :key="key">
            <dt>{{ key }}</dt>
            <dd>{{ value }}</dd>
          </template>
        </dl>
        <p v-else class="empty-state">未选择 Run</p>
        <button
          v-if="agent"
          class="chip-btn"
          type="button"
          @click="emit('export', agent.agent_id)"
        >
          导出整段对话
        </button>
      </template>

      <template v-else-if="tab === 'text'">
        <div class="block-head">
          <span class="eyebrow">任务原文</span>
          <button
            class="chip-btn"
            type="button"
            :disabled="!view?.run?.request_text"
            @click="copyText(view?.run?.request_text, '原文已复制')"
          >
            复制
          </button>
        </div>
        <pre class="raw-box tall">{{ view?.run?.request_text || "（无）" }}</pre>
      </template>

      <template v-else>
        <div class="block-head">
          <span class="eyebrow">原始 messages</span>
          <button class="chip-btn" type="button" :disabled="!rawMessages" @click="copyText(rawMessages)">
            复制
          </button>
        </div>
        <pre v-if="rawMessages" class="raw-box tall">{{ rawMessages }}</pre>
        <p v-else class="empty-state">该 Run 没有原始 messages 记录。</p>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: var(--detail-width);
  background: var(--bg-sidebar);
  border-left: 1px solid var(--line);
  box-shadow: var(--shadow-panel);
  display: flex;
  flex-direction: column;
  transform: translateX(calc(100% + 40px));
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  z-index: 300;
}
.drawer.is-open {
  transform: translateX(0);
}

.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 10px 10px 18px;
  border-bottom: 1px solid var(--line);
  min-height: 52px;
}
.drawer-title h2 {
  margin: 3px 0 0;
  font-size: 13.5px;
  font-family: var(--font-mono);
  font-weight: 600;
  word-break: break-all;
}

.tab-row {
  display: flex;
  border-bottom: 1px solid var(--line);
  padding: 0 8px;
}
.tab {
  flex: 1;
  padding: 9px 0;
  font-size: 12px;
  color: var(--text-muted);
  border-bottom: 2px solid transparent;
}
.tab:hover {
  color: var(--text-main);
}
.tab.is-active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
}
.drawer-body > .raw-box,
.drawer-body > .facts {
  width: 100%;
}

.facts {
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 8px 12px;
  margin: 0;
}
.facts dt {
  color: var(--text-muted);
  font-size: var(--size-meta);
}
.facts dd {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11.5px;
  word-break: break-all;
}

.block-head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.raw-box.tall {
  max-height: none;
  flex: 1;
  min-height: 200px;
}
</style>
