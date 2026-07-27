import { onBeforeUnmount } from "vue";

import { parseSsePayload, subscribeSSE, unsubscribeSSE } from "@/api/bridge";
import { applyRuntimeTrace, loadRuntimeAgents, refresh, state } from "@/store/console";

/** SSE 活着时轮询只当心跳；断了才回落到 5 秒。 */
const POLL_INTERVAL_LIVE_MS = 60000;
const POLL_INTERVAL_FALLBACK_MS = 5000;

/**
 * 同步调度：SSE 订阅 + 兜底轮询。
 *
 * 相比 1.4.1 的两点关键变化：
 * 1. runtime_trace 事件按 task 合并、用 rAF 统一 flush。后端每次
 *    _persist_runner_step 有新消息就推一次全量 tool_chain，agent 跑起来时
 *    是每秒数次；1.4.1 每收一条就整块重绘一次 feed。
 * 2. 页面不可见时暂停轮询，可见时立刻补一次。
 */
export function useConsoleSync() {
  let subscriptionId = "";
  let pollTimer = 0;
  let rafId = 0;
  const pendingTraces = new Map();

  function setStreamState(next) {
    if (state.streamState === next) return;
    state.streamState = next;
    startPolling();
  }

  function flushTraces() {
    rafId = 0;
    for (const payload of pendingTraces.values()) applyRuntimeTrace(payload);
    pendingTraces.clear();
  }

  function queueTrace(payload) {
    // 同一 run 的多条事件只保留最新一条：后端推的是全量快照，不是增量。
    pendingTraces.set(`${payload.agent_id}:${payload.task_id}`, payload);
    if (rafId) return;
    rafId = window.requestAnimationFrame(flushTraces);
  }

  async function handleMessage(event) {
    const data = parseSsePayload(event);
    if (!data) return;

    if (data.type === "closed") {
      setStreamState("poll");
      return;
    }
    if (data.type === "reset") {
      await refresh({ silent: true, keepSession: true });
      return;
    }
    if (data.type === "runtime_title" && data.agent_id) {
      await loadRuntimeAgents();
      return;
    }
    if (data.type === "runtime_trace") {
      queueTrace(data);
    }
  }

  function startPolling() {
    window.clearInterval(pollTimer);
    if (document.visibilityState === "hidden") return;
    const interval =
      state.streamState === "live" ? POLL_INTERVAL_LIVE_MS : POLL_INTERVAL_FALLBACK_MS;
    pollTimer = window.setInterval(() => {
      refresh({ silent: true, keepSession: true });
    }, interval);
  }

  function handleVisibility() {
    if (document.visibilityState === "hidden") {
      window.clearInterval(pollTimer);
      pollTimer = 0;
      return;
    }
    if (rafId === 0 && pendingTraces.size) flushTraces();
    startPolling();
    refresh({ silent: true, keepSession: true });
  }

  async function subscribe() {
    try {
      subscriptionId = await subscribeSSE("console/stream", {
        onOpen: () => setStreamState("live"),
        onMessage: handleMessage,
        onError: () => setStreamState("poll"),
      });
      if (!subscriptionId) setStreamState("poll");
    } catch {
      setStreamState("poll");
    }
  }

  function stop() {
    window.clearInterval(pollTimer);
    pollTimer = 0;
    window.cancelAnimationFrame(rafId);
    rafId = 0;
    document.removeEventListener("visibilitychange", handleVisibility);
    window.removeEventListener("beforeunload", stop);
    if (subscriptionId) {
      unsubscribeSSE(subscriptionId);
      subscriptionId = "";
    }
  }

  function start() {
    setStreamState("poll");
    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("beforeunload", stop);
    return subscribe();
  }

  onBeforeUnmount(stop);

  return { start, stop };
}
