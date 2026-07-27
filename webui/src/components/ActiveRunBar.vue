<script setup>
import { computed } from "vue";

import { useClock } from "@/composables/useClock";
import { formatDuration } from "@/utils/format";

const props = defineProps({
  view: { type: Object, required: true },
  title: { type: String, default: "" },
});

const emit = defineEmits(["stop", "jump"]);

const now = useClock();

const duration = computed(() => formatDuration(props.view.startedAt, now.value));

/** 当前工具：最后一个还没拿到结果的 tool step，否则退回最后一个 tool step。 */
const currentStep = computed(() => {
  const tools = props.view.steps.filter((step) => step.type === "tool");
  const running = [...tools].reverse().find((step) => step.result === null);
  return running || tools[tools.length - 1] || null;
});

const currentLabel = computed(() => {
  const step = currentStep.value;
  if (!step) return "等待管家响应…";
  const args = step.argsSummary ? `(${step.argsSummary})` : "()";
  return `${step.name || "工具"}${args}`;
});
</script>

<template>
  <div class="active-bar">
    <span class="status-dot is-running" aria-hidden="true" />
    <button class="active-title" type="button" @click="emit('jump')">
      {{ title }}
    </button>
    <span class="active-sep">·</span>
    <span class="active-duration">{{ duration }}</span>
    <span class="active-current" :title="currentLabel">{{ currentLabel }}</span>
    <button class="chip-btn is-danger" type="button" @click="emit('stop')">停止</button>
  </div>
</template>

<style scoped>
.active-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-app));
  font-size: var(--size-meta);
  color: var(--text-muted);
  flex-shrink: 0;
}
.active-title {
  font-weight: 600;
  color: var(--text-main);
  max-width: 30%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.active-title:hover {
  color: var(--accent);
}
.active-sep {
  color: var(--text-dim);
}
.active-duration {
  font-family: var(--font-mono);
  color: var(--text-main);
}
.active-current {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  color: var(--accent);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
