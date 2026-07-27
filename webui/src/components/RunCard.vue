<script setup>
import { computed, onBeforeUnmount, ref } from "vue";

import MarkdownBody from "@/components/MarkdownBody.vue";
import TraceStep from "@/components/TraceStep.vue";
import { useClock } from "@/composables/useClock";
import { copyText } from "@/composables/useCopy";
import { displayUserText, formatDuration } from "@/utils/format";
import { STOPPABLE_STATUSES, countToolSteps } from "@/utils/trace";

const props = defineProps({
  view: { type: Object, required: true },
  ordinal: { type: Number, required: true },
  mistressName: { type: String, default: "" },
});

const emit = defineEmits(["stop", "rewind", "fork", "result", "inspect"]);

const now = useClock();

/** 步骤很多时默认只展示尾部，头部折叠。局部状态，不受数据刷新影响。 */
const TAIL_STEPS = 14;
const showAllSteps = ref(false);
const showFullRequest = ref(false);

const toolCount = computed(() => countToolSteps(props.view.steps));

const hiddenStepCount = computed(() =>
  showAllSteps.value ? 0 : Math.max(0, props.view.steps.length - TAIL_STEPS),
);
const shownSteps = computed(() =>
  hiddenStepCount.value > 0 ? props.view.steps.slice(-TAIL_STEPS) : props.view.steps,
);

const duration = computed(() =>
  formatDuration(props.view.startedAt, props.view.active ? now.value : props.view.endedAt),
);

const canStop = computed(() => STOPPABLE_STATUSES.has(props.view.status));
const pendingNotification = computed(() => {
  const notification = props.view.run?.notification;
  return Boolean(notification && !notification.delivered);
});

const requestFull = computed(() => String(props.view.userText || ""));
const requestShown = computed(() =>
  showFullRequest.value ? requestFull.value : displayUserText(requestFull.value),
);
const requestTruncated = computed(() => requestShown.value !== requestFull.value);

/* 回溯会改变下次 resume 的上下文，且没有反向操作，所以要求点两次确认。
   与侧栏删除 Agent 用的是同一套「二次点击」约定。 */
const rewindArmed = ref(false);
let rewindArmTimer = 0;

function onRewindClick() {
  if (!rewindArmed.value) {
    rewindArmed.value = true;
    window.clearTimeout(rewindArmTimer);
    rewindArmTimer = window.setTimeout(() => {
      rewindArmed.value = false;
    }, 3200);
    return;
  }
  window.clearTimeout(rewindArmTimer);
  rewindArmed.value = false;
  emit("rewind");
}

onBeforeUnmount(() => window.clearTimeout(rewindArmTimer));
</script>

<template>
  <article
    class="run-card"
    :class="{ 'is-active': view.active, 'is-failed': view.failed, 'is-rewound': view.rewound }"
  >
    <header class="run-head">
      <span class="run-no">RUN {{ ordinal }}</span>
      <span class="status-dot" :class="`is-${view.status}`" aria-hidden="true" />
      <span class="run-status">{{ view.active ? "运行中" : view.status }}</span>
      <span v-if="toolCount" class="run-meta">{{ toolCount }} 工具</span>
      <span v-if="duration" class="run-meta">{{ duration }}</span>
      <span v-if="view.rewound" class="run-badge" title="已回溯：保留在记录里，但不再进入下次 resume 的上下文">
        已回溯
      </span>

      <span class="run-spacer" />

      <div class="run-actions">
        <button v-if="canStop" class="chip-btn is-danger" type="button" @click="emit('stop')">
          停止
        </button>
        <button v-if="pendingNotification" class="chip-btn" type="button" @click="emit('result')">
          读取结果
        </button>

        <button
          class="chip-btn"
          type="button"
          :disabled="!view.result && !requestFull"
          :title="view.result ? '复制本轮结果' : '复制本轮请求'"
          @click="
            view.result
              ? copyText(view.result, '结果已复制')
              : copyText(requestFull, '请求已复制')
          "
        >
          复制
        </button>
        <button
          v-if="!view.rewound"
          class="chip-btn"
          :class="{ 'is-danger': rewindArmed }"
          type="button"
          :disabled="view.active"
          :title="
            view.active
              ? '运行中无法回溯，请先停止'
              : '丢弃本轮及之后的上下文，回到本轮开始前的状态'
          "
          @click="onRewindClick"
        >
          {{ rewindArmed ? "确认回溯" : "回溯到这里" }}
        </button>
        <button
          class="chip-btn"
          type="button"
          :disabled="!requestFull"
          title="用同一条请求新建一个 Agent，不带任何上下文"
          @click="emit('fork')"
        >
          Fork
        </button>

        <button class="chip-btn" type="button" @click="emit('inspect')">详情</button>
      </div>
    </header>

    <div class="run-body">
      <div v-if="requestFull" class="run-request">
        <p class="run-request-text">{{ requestShown }}</p>
        <button
          v-if="requestTruncated || showFullRequest"
          class="run-inline-toggle"
          type="button"
          @click="showFullRequest = !showFullRequest"
        >
          {{ showFullRequest ? "收起" : "展开完整请求" }}
        </button>
      </div>

      <div v-if="view.mistressText" class="run-mistress">
        <span class="run-mistress-label">{{ mistressName || "大小姐" }}</span>
        <p>{{ view.mistressText }}</p>
      </div>

      <div v-if="view.steps.length" class="run-trace">
        <button
          v-if="hiddenStepCount"
          class="run-inline-toggle is-block"
          type="button"
          @click="showAllSteps = true"
        >
          ▸ 展开前 {{ hiddenStepCount }} 步
        </button>
        <TraceStep
          v-for="step in shownSteps"
          :key="step.key"
          :step="step"
          :task-active="view.active"
        />
      </div>

      <p v-if="view.active" class="run-pending">
        <span class="status-dot is-running" aria-hidden="true" />执行中…
      </p>

      <div v-if="view.result" class="run-result">
        <MarkdownBody :text="view.result" />
      </div>
      <p v-else-if="view.failed" class="run-error">
        {{ view.error || "任务发生异常，已终止。" }}
      </p>

      <div v-for="(steer, index) in view.steers" :key="`steer-${index}`" class="run-steer">
        <span class="run-steer-label">追加</span>
        <p>{{ steer }}</p>
      </div>
    </div>
  </article>
</template>

<style scoped>
.run-card {
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  background: var(--bg-raised);
  overflow: hidden;
}
.run-card.is-active {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--line));
}
.run-card.is-failed {
  border-color: color-mix(in srgb, var(--err) 40%, var(--line));
}
/* 已回溯：留在时间线上作为记录，但明显退到背景里。 */
.run-card.is-rewound {
  border-style: dashed;
  opacity: 0.55;
}
.run-card.is-rewound:hover,
.run-card.is-rewound:focus-within {
  opacity: 1;
}
.run-badge {
  padding: 1px 7px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: var(--size-meta);
  color: var(--text-muted);
}

.run-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 7px 12px;
  background: var(--bg-sunken);
  border-bottom: 1px solid var(--line);
  font-size: var(--size-meta);
  color: var(--text-muted);
}
.run-no {
  font-family: var(--font-mono);
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--text-main);
}
.run-status {
  font-family: var(--font-mono);
  color: var(--text-main);
}
.run-meta::before {
  content: "·";
  margin-right: 8px;
  color: var(--text-dim);
}
.run-spacer {
  flex: 1;
  min-width: 12px;
}
.run-actions {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.run-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
}

.run-request-text,
.run-mistress p,
.run-steer p {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.run-request {
  font-size: var(--size-body);
  line-height: 1.65;
  color: var(--text-main);
}

.run-mistress,
.run-steer {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 7px 10px;
  border-radius: var(--radius-item);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  font-size: 13.5px;
  line-height: 1.6;
}
.run-mistress-label,
.run-steer-label {
  flex-shrink: 0;
  font-size: var(--size-meta);
  font-weight: 600;
  color: var(--accent);
}

.run-trace {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-item);
  background: var(--bg-sunken);
}

.run-inline-toggle {
  align-self: flex-start;
  font-size: var(--size-meta);
  color: var(--accent);
  padding: 2px 0;
}
.run-inline-toggle:hover {
  text-decoration: underline;
}
.run-inline-toggle.is-block {
  font-family: var(--font-mono);
  margin-bottom: 3px;
}

.run-pending {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--size-mono);
  color: var(--accent);
}

.run-error {
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--radius-item);
  background: var(--err-soft);
  color: var(--err);
  font-size: 13.5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
