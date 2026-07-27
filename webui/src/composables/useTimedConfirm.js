import { onBeforeUnmount, ref } from "vue";

const DEFAULT_MS = 3200;

/**
 * 单按钮两次点击确认。
 *
 * 第一次触发进入 armed 态（按钮文案/样式变红），第二次才真正执行；超时自动 disarm。
 * 返回的 trigger 在应当执行破坏性动作时返回 true，调用方据此 emit。
 *
 * 用在 RunCard 回溯、SessionSidebar 删除 Agent —— 两处都是「同一按钮两次点击」模型，
 * 之前各写一份定时器，现在收敛到一个地方。
 */
export function useTimedConfirm(ms = DEFAULT_MS) {
  const armed = ref(false);
  let timer = 0;

  function trigger() {
    if (!armed.value) {
      armed.value = true;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        armed.value = false;
      }, ms);
      return false;
    }
    window.clearTimeout(timer);
    armed.value = false;
    return true;
  }

  function disarm() {
    armed.value = false;
    window.clearTimeout(timer);
  }

  onBeforeUnmount(disarm);
  return { armed, trigger, disarm };
}
