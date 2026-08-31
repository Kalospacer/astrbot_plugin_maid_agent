import { useSyncExternalStore } from "react";

import { getSnapshot, getVersion, subscribe } from "@/store/app";

export function useApp<T>(selector: (state: ReturnType<typeof getSnapshot>) => T): T {
  useSyncExternalStore(subscribe, getVersion, getVersion);
  return selector(getSnapshot());
}

export function useAppVersion(): number {
  return useSyncExternalStore(subscribe, getVersion, getVersion);
}
