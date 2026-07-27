<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";

import RunCard from "@/components/RunCard.vue";
import { useStickyScroll } from "@/composables/useStickyScroll";

const props = defineProps({
  views: { type: Array, default: () => [] },
  mistressName: { type: String, default: "" },
  agentId: { type: String, default: "" },
});

const emit = defineEmits(["stop", "rewind", "fork", "result", "inspect"]);

const scroller = ref(null);
const sentinel = ref(null);

/**
 * 内容增长信号：步骤数 + 追加消息数 + 结果字符数。
 * 只要它单调上涨就说明有新内容；用户展开一个 trace 不会改变它，
 * 所以展开不会触发任何滚动。
 */
const growth = computed(() =>
  props.views.reduce(
    (total, view) => total + view.steps.length + view.steers.length + view.result.length,
    0,
  ),
);

const { stick, pending, scrollToBottom } = useStickyScroll(scroller, sentinel, () => growth.value);

// 首次挂载 / 切换会话：回到底部并清空未读计数。
// 这是显式的用户动作（打开一个会话），不是后台数据更新。
onMounted(async () => {
  await nextTick();
  scrollToBottom({ smooth: false });
});

watch(
  () => props.agentId,
  async () => {
    await nextTick();
    scrollToBottom({ smooth: false });
  },
);

defineExpose({ scrollToBottom });
</script>

<template>
  <div class="timeline-wrap">
    <div ref="scroller" class="timeline-scroll">
      <div class="timeline-inner">
        <p v-if="!views.length" class="empty-state">
          该 Agent 暂无 Run 记录。在下面输入要求即可派活。
        </p>

        <RunCard
          v-for="view in views"
          :key="view.taskId"
          :view="view"
          :ordinal="view.ordinal"
          :mistress-name="mistressName"
          @stop="emit('stop', view)"
          @rewind="emit('rewind', view)"
          @fork="emit('fork', view)"
          @result="emit('result', view)"
          @inspect="emit('inspect', view)"
        />

        <div ref="sentinel" class="timeline-sentinel" aria-hidden="true" />
      </div>
    </div>

    <transition name="pill">
      <button
        v-if="!stick && pending > 0"
        class="new-content-pill"
        type="button"
        @click="scrollToBottom({ smooth: true })"
      >
        ↓ {{ pending }} 条新内容
      </button>
    </transition>
  </div>
</template>

<style scoped>
.timeline-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}
.timeline-scroll {
  flex: 1;
  overflow-y: auto;
  /* 刻意不设 scroll-behavior：程序化滚动的平滑与否由调用点决定，
     否则每次贴底跟随都会变成一段可见的动画。 */
}
.timeline-inner {
  max-width: var(--content-width);
  margin: 0 auto;
  padding: 14px 20px 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.timeline-sentinel {
  height: 1px;
  flex-shrink: 0;
}

.new-content-pill {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  background: var(--bg-raised);
  color: var(--text-main);
  font-size: 12.5px;
  box-shadow: var(--shadow-float);
  z-index: 20;
}
.new-content-pill:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.pill-enter-active,
.pill-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.pill-enter-from,
.pill-leave-to {
  opacity: 0;
  transform: translate(-50%, 6px);
}

@media (max-width: 860px) {
  .timeline-inner {
    padding: 12px 12px 8px;
  }
}
</style>
