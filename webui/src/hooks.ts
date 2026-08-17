import { useSyncExternalStore } from "react";

import { getSnapshot, getVersion, subscribe } from "@/store/app";

/**
 * 应用 store 绑定。
 *
 * store 对 SessionState 是就地变更（events.set 不换引用），所以快照必须钉在
 * 单调递增的 version 上：emit 时 version 变 → 订阅组件重渲染 → selector 重算。
 * 若直接把 selector 结果当快照，就地变更对 Object.is 比较不可见（事件不渲染），
 * 而不稳定 selector 又会陷入死循环（每次返回新引用 → 反复重渲染）。
 */
export function useApp<T>(selector: (state: ReturnType<typeof getSnapshot>) => T): T {
  useSyncExternalStore(subscribe, getVersion, getVersion);
  return selector(getSnapshot());
}

export function useAppVersion(): number {
  return useSyncExternalStore(subscribe, getVersion, getVersion);
}
