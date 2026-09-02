import { useRef } from "react";
import { useSyncExternalStore } from "react";

import { getSnapshot, getVersion, subscribe } from "@/store/app";

type AppSnapshot = ReturnType<typeof getSnapshot>;

interface SelectionCache<T> {
  version: number;
  value: T;
}

/**
 * 选择器订阅：只有当选中切片发生 Object.is 意义上的变化时才触发重渲染。
 *
 * 旧实现把全局 version 当作 snapshot，任何一次 emit（流式输出时每秒数十次）
 * 都会重渲染整棵组件树。这里为每个 hook 实例缓存「version + 选中值」：
 * version 未变直接返回缓存；version 变了但选中值 Object.is 相等时沿用旧引用，
 * useSyncExternalStore 因而不会安排重渲染。
 *
 * 约束：selector 必须返回原始值或稳定引用（store 内部原地修改的对象请通过
 * 对应的 stamp 字段订阅，例如 session.stamp / state.sessionsStamp）。
 */
export function useApp<T>(selector: (state: AppSnapshot) => T): T {
  const cacheRef = useRef<SelectionCache<T> | null>(null);
  const selectorRef = useRef(selector);
  selectorRef.current = selector;

  const getSelected = (): T => {
    const version = getVersion();
    const cache = cacheRef.current;
    if (cache !== null && cache.version === version) return cache.value;
    const value = selectorRef.current(getSnapshot());
    if (cache !== null && Object.is(cache.value, value)) {
      cache.version = version;
      return cache.value;
    }
    cacheRef.current = { version, value };
    return value;
  };

  return useSyncExternalStore(subscribe, getSelected, getSelected);
}

export function useAppVersion(): number {
  return useSyncExternalStore(subscribe, getVersion, getVersion);
}
