import { onBeforeUnmount, readonly, ref } from "vue";

/**
 * 共享的 1 秒时钟，供「已跑 xx」这类实时时长使用。
 * 用引用计数开关，没有组件订阅时不占定时器。
 */
const now = ref(new Date().toISOString());
let timer = 0;
let subscribers = 0;

function start() {
  if (timer) return;
  timer = window.setInterval(() => {
    now.value = new Date().toISOString();
  }, 1000);
}

function stop() {
  window.clearInterval(timer);
  timer = 0;
}

export function useClock() {
  subscribers += 1;
  now.value = new Date().toISOString();
  start();

  onBeforeUnmount(() => {
    subscribers -= 1;
    if (subscribers <= 0) {
      subscribers = 0;
      stop();
    }
  });

  return readonly(now);
}
