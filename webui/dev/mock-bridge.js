/**
 * 假的 window.AstrBotPluginPage，只在 `npm run dev` 且不在 dashboard iframe 里时装载。
 *
 * 存在的意义是能在本地把「后台刷新会不会动视口 / 会不会抢焦点 / 展开态会不会丢」
 * 这些行为跑出来验证，不用起一整套 AstrBot。
 *
 * window.__maidMock 暴露给自动化断言用（推进 trace、加速轮询等）。
 */

import { advanceLiveRun, createWorld } from "./fixtures.js";

const LATENCY_MS = 120;

function delay(ms = LATENCY_MS) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function installMockBridge() {
  const world = createWorld();
  const sseHandlers = new Map();
  let subscriptionSeq = 0;
  let liveTimer = 0;

  function broadcast(payload) {
    for (const handlers of sseHandlers.values()) {
      handlers.onMessage?.({ parsed: payload, raw: JSON.stringify(payload) });
    }
  }

  function pushLiveStep() {
    if (!world.liveTaskId) return;
    const { entries } = advanceLiveRun(world);
    broadcast({
      type: "runtime_trace",
      agent_id: world.liveAgentId,
      task_id: world.liveTaskId,
      status: "running",
      tool_chain: { entries, messages: [] },
    });
  }

  function startLive(intervalMs = 1500) {
    window.clearInterval(liveTimer);
    liveTimer = window.setInterval(pushLiveStep, intervalMs);
  }

  function stopLive() {
    window.clearInterval(liveTimer);
    liveTimer = 0;
  }

  function matchPath(endpoint) {
    const clean = String(endpoint || "").replace(/^\/+/, "");
    const parts = clean.split("/");
    return { clean, parts };
  }

  async function apiGet(endpoint) {
    await delay();
    const { clean, parts } = matchPath(endpoint);

    if (clean === "console/overview") return { config: world.config, stats: {} };
    if (clean === "console/settings") return { config: world.config };
    if (clean === "console/subagents") return { agents: world.subagents };
    if (clean === "console/agents") return { agents: world.agents.map((a) => ({ ...a })) };

    // console/agents/<agent_id>/...
    if (parts[0] === "console" && parts[1] === "agents" && parts[2]) {
      const agentId = decodeURIComponent(parts[2]);
      if (parts[3] === "transcript") {
        const transcript = world.transcripts[agentId];
        if (!transcript) throw new Error("Agent 不存在");
        return JSON.parse(JSON.stringify(transcript));
      }
      if (parts[3] === "runs" && !parts[4]) {
        return { runs: JSON.parse(JSON.stringify(world.runs[agentId] || [])) };
      }
      if (parts[3] === "runs" && parts[5] === "trace") {
        const taskId = decodeURIComponent(parts[4]);
        const segment = (world.transcripts[agentId]?.runs || []).find((s) => s.task_id === taskId);
        const entries = segment?.tool_chain?.entries || [];
        return {
          tool_chain: {
            entries,
            messages: entries.map((entry) => ({
              role: entry.kind,
              tool_call_id: entry.tool_call_id,
              content: entry.message ?? entry.arguments ?? null,
            })),
          },
        };
      }
    }

    throw new Error(`mock bridge: 未实现的 GET ${clean}`);
  }

  async function apiPost(endpoint, body) {
    await delay();
    const { clean, parts } = matchPath(endpoint);

    if (clean === "console/settings") {
      Object.assign(world.config, body);
      return { config: world.config };
    }
    if (clean === "console/actions/stop") {
      const run = world.runs[world.liveAgentId]?.find((r) => r.task_id === body.task_id);
      if (run) {
        run.status = "stopped";
        run.ended_at = new Date().toISOString();
      }
      const agent = world.agents.find((a) => a.agent_id === world.liveAgentId);
      if (agent) {
        agent.active_task_id = "";
        agent.last_status = "stopped";
      }
      stopLive();
      return { outcome: { status: "stopped" } };
    }
    if (clean === "console/actions/steer") {
      const segment = world.transcripts[body.agent_id]?.runs.find((s) => s.task_id === body.task_id);
      segment?.steers.push(body.message_text);
      return { outcome: { status: "steered" } };
    }
    if (clean === "console/actions/resume" || clean === "console/actions/dispatch") {
      return { outcome: { agent_id: body.agent_id || world.liveAgentId, status: "queued" } };
    }
    if (clean === "console/actions/rerun") {
      return { outcome: { agent_id: world.liveAgentId, status: "queued" } };
    }
    if (clean === "console/actions/result") {
      return { outcome: { query_status: "completed" } };
    }
    if (parts[3] === "delete") {
      const agentId = decodeURIComponent(parts[2]);
      world.agents = world.agents.filter((a) => a.agent_id !== agentId);
      delete world.runs[agentId];
      delete world.transcripts[agentId];
      return {};
    }

    throw new Error(`mock bridge: 未实现的 POST ${clean}`);
  }

  window.AstrBotPluginPage = {
    ready: () => Promise.resolve({ theme: document.documentElement.dataset.theme || "light" }),
    apiGet,
    apiPost,
    async upload(_endpoint, file) {
      await delay(400);
      return { path: `/mock/uploads/${file?.name || "image.png"}`, size: file?.size || 0 };
    },
    async download(_endpoint, _params, filename) {
      await delay();
      console.info("[mock bridge] download", filename);
    },
    async subscribeSSE(_endpoint, handlers) {
      const id = `mock_sse_${(subscriptionSeq += 1)}`;
      sseHandlers.set(id, handlers);
      setTimeout(() => handlers.onOpen?.(), 50);
      startLive();
      return id;
    },
    async unsubscribeSSE(id) {
      sseHandlers.delete(id);
      if (!sseHandlers.size) stopLive();
    },
  };

  // 自动化断言用的钩子
  window.__maidMock = {
    world,
    pushLiveStep,
    startLive,
    stopLive,
    broadcast,
    /** 一次性推 n 步，用来模拟高频 trace 洪水 */
    burst(count = 10) {
      for (let i = 0; i < count; i += 1) pushLiveStep();
    },
  };

  console.info("[mock bridge] 已装载。window.__maidMock 可用。");
}
