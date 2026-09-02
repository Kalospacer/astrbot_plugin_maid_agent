// 弹窗外点击关闭（DSH ui-primitives useDismissOnOutsidePointer 原样移植）：
// 表面打开期间，pointerdown 落在 root（或可选 portal）之外时关闭。

import { useEffect } from "react";
import type { RefObject } from "react";

export function useDismissOnOutsidePointer(
  root: RefObject<HTMLElement | null>,
  open: boolean,
  setOpen: (open: boolean) => void,
  portal?: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent): void => {
      if (
        event.target instanceof Node &&
        root.current?.contains(event.target) !== true &&
        portal?.current?.contains(event.target) !== true
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [root, open, setOpen, portal]);
}
