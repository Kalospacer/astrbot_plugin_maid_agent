<script setup>
import { computed, ref } from "vue";

import { copyText } from "@/composables/useCopy";
import { traceStepStatus, truncateTraceText } from "@/utils/trace";

const props = defineProps({
  step: { type: Object, required: true },
  taskActive: { type: Boolean, default: false },
});

/**
 * 展开态是**组件自己的**局部状态。
 *
 * 父层用 step.key 做 v-for 的 key，实例在数据刷新时被复用，所以这里的 open
 * 不会因为轮询或 SSE 推送而丢失。1.4.1 把展开态存进全局 map 又拼进渲染指纹，
 * 结果是展开一个工具调用反过来触发整块 feed 重绘。
 */
const open = ref(false);

const status = computed(() => traceStepStatus(props.step, props.taskActive));
const isText = computed(() => props.step.type === "text");

const resultText = computed(() => {
  const value = props.step.result;
  return value === null || value === undefined ? "" : String(value);
});

const textPreview = computed(() => truncateTraceText(props.step.text, { lines: 3, chars: 300 }));
const resultPreview = computed(() => truncateTraceText(resultText.value, { lines: 2, chars: 180 }));

const expandable = computed(() => {
  if (isText.value) return textPreview.value.truncated;
  return Boolean(props.step.argsText) || Boolean(resultText.value.trim());
});

const toolLabel = computed(() => props.step.name || props.step.callId || "工具");

function toggle() {
  if (expandable.value) open.value = !open.value;
}
</script>

<template>
  <div class="trace-step" :class="[`is-${status}`, { 'is-open': open }]">
    <component
      :is="expandable ? 'button' : 'div'"
      class="trace-head"
      :type="expandable ? 'button' : undefined"
      :aria-expanded="expandable ? String(open) : undefined"
      @click="toggle"
    >
      <span class="trace-dot" aria-hidden="true" />

      <span v-if="isText" class="trace-text">
        {{ open ? step.text : textPreview.preview
        }}<span v-if="textPreview.truncated && !open" class="trace-more">
          … +{{ textPreview.remainLines }} 行</span
        >
      </span>

      <span v-else class="trace-tool">
        <span class="trace-tool-name">{{ toolLabel }}</span>
        <span class="trace-tool-args">({{ step.argsSummary }})</span>
        <span v-if="!open && resultPreview.preview" class="trace-result-peek">
          {{ resultPreview.preview
          }}<span v-if="resultPreview.truncated" class="trace-more">…</span>
        </span>
        <span v-else-if="!open && taskActive && !resultText" class="trace-result-peek is-waiting">
          执行中…
        </span>
      </span>

      <span v-if="expandable" class="trace-caret" aria-hidden="true">{{ open ? "▾" : "▸" }}</span>
    </component>

    <div v-if="open && !isText" class="trace-detail">
      <div v-if="step.argsText" class="trace-block">
        <div class="trace-block-head">
          <span class="eyebrow">参数</span>
          <button class="chip-btn" type="button" @click="copyText(step.argsText, '参数已复制')">
            复制
          </button>
        </div>
        <pre class="raw-box">{{ step.argsText }}</pre>
      </div>
      <div v-if="resultText.trim()" class="trace-block">
        <div class="trace-block-head">
          <span class="eyebrow">结果</span>
          <button class="chip-btn" type="button" @click="copyText(resultText, '结果已复制')">
            复制
          </button>
        </div>
        <pre class="raw-box">{{ resultText }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.trace-step {
  font-family: var(--font-mono);
  font-size: var(--size-mono);
  line-height: 1.65;
  border-left: 2px solid transparent;
  padding-left: 8px;
  margin-left: -10px;
}
.trace-step.is-error {
  border-left-color: var(--err);
}

.trace-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 2px 6px 2px 2px;
  border-radius: 5px;
  color: var(--text-main);
}
button.trace-head:hover {
  background: var(--bg-sunken);
}

.trace-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 7px;
  background: var(--text-dim);
}
.is-ok .trace-dot {
  background: var(--ok);
}
.is-error .trace-dot {
  background: var(--err);
}
.is-dead .trace-dot {
  background: var(--text-dim);
  opacity: 0.5;
}
.is-running .trace-dot {
  background: var(--accent);
  animation: dot-pulse 1.2s ease-in-out infinite;
}
.is-text .trace-dot {
  background: var(--text-dim);
  opacity: 0.7;
}

.trace-text {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-muted);
}

.trace-tool {
  flex: 1;
  min-width: 0;
  display: block;
}
.trace-tool-name {
  font-weight: 600;
  color: var(--text-main);
}
.trace-tool-args {
  color: var(--text-muted);
  word-break: break-all;
}
.trace-result-peek {
  display: block;
  margin-top: 1px;
  padding-left: 10px;
  border-left: 1px solid var(--line);
  color: var(--text-dim);
  white-space: pre-wrap;
  word-break: break-word;
}
.trace-result-peek.is-waiting {
  color: var(--accent);
  font-style: italic;
}
.trace-more {
  color: var(--accent);
}

.trace-caret {
  flex-shrink: 0;
  color: var(--text-dim);
  font-size: 10px;
  margin-top: 4px;
}

.trace-detail {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 6px 0 10px 15px;
}
.trace-block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}
</style>
