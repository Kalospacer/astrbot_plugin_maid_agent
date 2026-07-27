import { nextTick, onBeforeUnmount, ref, watch } from "vue";

/**
 * 滚动意图状态机。
 *
 * 1.4.1 的做法是每次轮询后无条件写 scrollTop，并用「距底 80px 内」当作
 * 「用户想贴底」，结果是停在任何靠近底部的位置都会被每 5 秒拽到底一次。
 *
 * 这里把「是否贴底」变成显式状态 `stick`，且**只由用户的滚动位置翻转**：
 * feed 末尾放一个 sentinel，用 IntersectionObserver 判定它是否在视口内。
 * 数据增长时，只有 stick 为真才跟随到底；否则累计 pending 计数交给
 * 「↓ N 条新内容」的 pill，由用户决定什么时候回到底部。
 *
 * @param {import('vue').Ref<HTMLElement|null>} containerRef 滚动容器
 * @param {import('vue').Ref<HTMLElement|null>} sentinelRef  容器末尾的哨兵元素
 * @param {() => number} growthSignal 内容量的单调信号（步骤数、字符数之类）
 */
export function useStickyScroll(containerRef, sentinelRef, growthSignal) {
  const stick = ref(true);
  const pending = ref(0);

  let observer = null;

  function disconnect() {
    observer?.disconnect();
    observer = null;
  }

  function observe() {
    disconnect();
    const root = containerRef.value;
    const sentinel = sentinelRef.value;
    if (!root || !sentinel) return;
    observer = new IntersectionObserver(
      (entries) => {
        const atBottom = entries[entries.length - 1]?.isIntersecting ?? false;
        stick.value = atBottom;
        if (atBottom) pending.value = 0;
      },
      // 底边外扩 48px：留一点容差，但远小于 1.4.1 那个会误伤的 80px 阈值
      { root, threshold: 0, rootMargin: "0px 0px 48px 0px" },
    );
    observer.observe(sentinel);
  }

  watch([containerRef, sentinelRef], observe, { flush: "post" });

  function scrollToBottom({ smooth = false } = {}) {
    const root = containerRef.value;
    if (!root) return;
    root.scrollTo({ top: root.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    stick.value = true;
    pending.value = 0;
  }

  // 内容增长：贴底就跟随，否则只记账。
  watch(
    growthSignal,
    async (now, before) => {
      const delta = Number(now) - Number(before ?? 0);
      if (!Number.isFinite(delta) || delta <= 0) return;
      await nextTick();
      if (stick.value) {
        scrollToBottom({ smooth: false });
      } else {
        pending.value += delta;
      }
    },
    { flush: "post" },
  );

  onBeforeUnmount(disconnect);

  return { stick, pending, scrollToBottom, refreshObserver: observe };
}
