import { computed, reactive } from "vue";

import { apiGet, apiPost, download } from "@/api/bridge";
import { toast, toastError } from "@/composables/useToast";
import { displayAgentTitle } from "@/utils/alias";
import { ACTIVE_STATUSES, FAILED_STATUSES, pairToolChainSteps } from "@/utils/trace";

/**
 * 控制台状态。
 *
 * 与 1.4.1 的关键差异：这里**只存服务端数据和用户选择**。
 * 「哪个 trace 展开着」「滚动到哪」这类 UI 本地状态归组件自己所有，
 * 不进 store —— 1.4.1 把展开态混进渲染指纹，导致展开一个工具调用
 * 就会触发下一轮轮询的全量重绘。
 */
export const state = reactive({
  // 连接
  streamState: "idle", // idle | live | poll | error
  lastSyncAt: "",

  // 服务端数据
  overview: null,
  settings: null,
  subagents: [],
  runtimeAgents: [],
  runs: {}, // agent_id -> RunMeta[]
  transcript: null, // 当前选中 agent 的 transcript
  runTrace: {}, // task_id -> { entries, messages }（抽屉里的原始 messages 用）

  // 选择
  umos: [],
  selectedUmo: "",
  selectedAgentId: "",
  dispatchAgent: "",

  // 视图
  view: "timeline", // timeline | docs
  sessionFilter: "",

  // 并发保护
  refreshInFlight: false,
  refreshQueued: false,
});

/* ------------------------------------------------------------------ 派生 */

export const selectedAgent = computed(
  () => state.runtimeAgents.find((agent) => agent.agent_id === state.selectedAgentId) || null,
);

export const configuredDefaultAgent = computed(
  () => state.settings?.default_agent_name || state.overview?.config?.default_agent_name || "butler",
);

export const agentNames = computed(() => [
  ...new Set([configuredDefaultAgent.value, ...state.subagents.map((a) => a.name)].filter(Boolean)),
]);

/**
 * 侧栏会话列表。
 *
 * 排序键是 created_at（降序）而不是 1.4.1 的 last_run_at/updated_at：
 * running agent 的 updated_at 每次轮询都在变，会让列表在指针底下重排。
 * 会话位置在其生命周期内恒定，运行状态靠状态点表达。
 */
export const visibleSessions = computed(() => {
  const keyword = state.sessionFilter.trim().toLowerCase();
  return state.runtimeAgents
    .filter((agent) => agent.unified_msg_origin === state.selectedUmo)
    .filter((agent) => {
      if (!keyword) return true;
      return [displayAgentTitle(agent), agent.agent_name, agent.agent_id]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    })
    .sort((a, b) => {
      const diff = new Date(b.created_at || 0) - new Date(a.created_at || 0);
      return diff !== 0 ? diff : String(a.agent_id).localeCompare(String(b.agent_id));
    });
});

/**
 * 当前会话的 run 时间线。transcript 的 segment 与 RunMeta 合并；
 * 刚派发、还没有 transcript 记录的 run 也补进来，这样点了发送立刻能看到卡片。
 */
export const runViews = computed(() => {
  const segments = state.transcript?.runs || [];
  const runs = state.runs[state.selectedAgentId] || [];
  const byTask = new Map(runs.map((run) => [run.task_id, run]));

  const views = segments.map((segment) => buildRunView(segment, byTask.get(segment.task_id)));

  const seen = new Set(views.map((view) => view.taskId));
  const orphanRuns = runs
    .filter((run) => !seen.has(run.task_id))
    .sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
  for (const run of orphanRuns) {
    views.push(buildRunView({ task_id: run.task_id, steers: [] }, run));
  }

  return views.map((view, index) => ({ ...view, ordinal: index + 1 }));
});

function buildRunView(segment, run) {
  const status = run?.status || "completed";
  return {
    taskId: segment.task_id,
    run: run || null,
    status,
    active: ACTIVE_STATUSES.has(status),
    failed: FAILED_STATUSES.has(status),
    userText: segment.user_text || "",
    mistressText: segment.mistress_text || "",
    steers: segment.steers || [],
    steps: pairToolChainSteps(segment.tool_chain?.entries),
    result: String(run?.result || ""),
    error: String(run?.error || ""),
    startedAt: run?.started_at || run?.created_at || "",
    endedAt: run?.ended_at || run?.completed_at || run?.updated_at || "",
  };
}

export const activeRunView = computed(() => runViews.value.find((view) => view.active) || null);

export const mistressName = computed(() => String(state.transcript?.mistress_name || "").trim());

/* ------------------------------------------------------------------ 读取 */

export async function loadOverview() {
  state.overview = await apiGet("console/overview");
  state.settings = state.overview.config || state.settings;
  if (!state.dispatchAgent && state.settings?.default_agent_name) {
    state.dispatchAgent = state.settings.default_agent_name;
  }
}

export async function loadSubagents() {
  const data = await apiGet("console/subagents");
  state.subagents = data.agents || [];
  if (!state.dispatchAgent || !agentNames.value.includes(state.dispatchAgent)) {
    state.dispatchAgent = agentNames.value[0] || configuredDefaultAgent.value;
  }
}

export async function loadRuntimeAgents() {
  const data = await apiGet("console/agents");
  state.runtimeAgents = data.agents || [];
  syncKnownUmos();
}

function syncKnownUmos() {
  const umoSet = new Set();
  for (const agent of state.runtimeAgents) {
    if (agent.unified_msg_origin) umoSet.add(agent.unified_msg_origin);
  }
  if (state.selectedUmo) umoSet.add(state.selectedUmo);
  state.umos = Array.from(umoSet).sort();
  if (!state.selectedUmo && state.umos.length > 0) {
    state.selectedUmo = state.umos[0];
  }
}

/**
 * 拉取 transcript + runs。
 *
 * 不再清空 runTrace 缓存 —— 1.4.1 每次都清，导致抽屉每轮轮询重新拉一遍 trace。
 * 缓存只在该 task 收到新的 SSE trace 事件时失效。
 */
export async function loadTranscript(agentId) {
  if (!agentId) return;
  const [transcriptData, runsData] = await Promise.all([
    apiGet(`console/agents/${encodeURIComponent(agentId)}/transcript`),
    apiGet(`console/agents/${encodeURIComponent(agentId)}/runs`),
  ]);
  if (state.selectedAgentId !== agentId) return; // 拉取期间用户已切走
  state.transcript = transcriptData;
  state.runs[agentId] = runsData.runs || [];
}

/** 抽屉的「原始 messages」按需拉取，一个 task 只拉一次。 */
export async function loadRunTrace(taskId) {
  const agentId = state.selectedAgentId;
  if (!agentId || !taskId || state.runTrace[taskId] !== undefined) return;
  state.runTrace[taskId] = null; // in-flight 占位，防重复拉取
  try {
    const data = await apiGet(
      `console/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(taskId)}/trace`,
    );
    state.runTrace[taskId] = data.tool_chain || { entries: [], messages: [] };
  } catch {
    delete state.runTrace[taskId];
  }
}

export async function refresh({ silent = true, keepSession = true } = {}) {
  if (state.refreshInFlight) {
    state.refreshQueued = true;
    return;
  }
  state.refreshInFlight = true;
  try {
    await loadRuntimeAgents();
    await Promise.allSettled([loadOverview(), loadSubagents()]);

    if (keepSession && state.selectedAgentId) {
      const exists = state.runtimeAgents.some((a) => a.agent_id === state.selectedAgentId);
      if (exists) {
        await loadTranscript(state.selectedAgentId);
      } else {
        clearSelection();
      }
    }
    state.lastSyncAt = new Date().toISOString();
    if (!silent) toast("已刷新");
  } catch (err) {
    state.streamState = "error";
    if (!silent) toastError(err, "刷新失败");
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      window.setTimeout(() => refresh({ silent: true, keepSession: true }), 200);
    }
  }
}

/* ------------------------------------------------------------------ 选择 */

function clearSelection() {
  state.selectedAgentId = "";
  state.transcript = null;
}

/** 选中态的唯一权威来源：先写选中，再异步拉数据；拉取失败不改选中。 */
export async function selectAgent(agentId) {
  state.view = "timeline";
  if (!agentId) {
    clearSelection();
    return;
  }
  state.selectedAgentId = agentId;
  state.transcript = null;
  try {
    await loadTranscript(agentId);
  } catch (err) {
    toastError(err, "加载对话失败");
  }
}

export function selectUmo(umo) {
  state.selectedUmo = umo;
  clearSelection();
}

/* ------------------------------------------------------------------ 动作 */

/**
 * 发送。按当前会话状态分派：
 * 有活跃 run → steer；有会话但空闲 → resume；无会话 → dispatch。
 * @returns {Promise<string>} 发送后应当选中的 agent_id
 */
export async function submitPrompt({ text, imagePaths = [] }) {
  const agent = selectedAgent.value;
  const umo = agent?.unified_msg_origin || state.selectedUmo;
  if (!umo) throw new Error("先选择或填写 UMO");

  const requestText = text || "请查看并处理附带的图片。";

  if (agent?.active_task_id) {
    if (imagePaths.length) {
      throw new Error("运行中的 steer 目前只支持文字；请等待完成后用 Resume 附图。");
    }
    await apiPost("console/actions/steer", {
      agent_id: agent.agent_id,
      task_id: agent.active_task_id,
      message_text: requestText,
    });
    await loadTranscript(agent.agent_id);
    toast("已 steer 当前 Agent");
    return agent.agent_id;
  }

  if (agent) {
    const res = await apiPost("console/actions/resume", {
      agent_id: agent.agent_id,
      request_text: requestText,
      unified_msg_origin: umo,
      image_urls_raw: imagePaths,
    });
    if (!res.outcome) throw new Error(res.error || "resume 失败");
    await refresh({ silent: true, keepSession: true });
    toast("已 Resume Agent");
    return res.outcome.agent_id || agent.agent_id;
  }

  const res = await apiPost("console/actions/dispatch", {
    unified_msg_origin: umo,
    agent_name: state.dispatchAgent || configuredDefaultAgent.value,
    request_text: requestText,
    run_in_background: true,
    image_urls_raw: imagePaths,
  });
  if (!res.outcome) throw new Error(res.error || "派发失败");
  await refresh({ silent: true, keepSession: false });
  toast("已创建 Agent Run");
  return res.outcome.agent_id;
}

export async function stopRun(taskId) {
  try {
    await apiPost("console/actions/stop", { task_id: taskId });
    toast("已请求停止");
    await refresh({ silent: true, keepSession: true });
  } catch (err) {
    toastError(err, "停止失败");
  }
}

export async function rerunRun(taskId) {
  try {
    const res = await apiPost("console/actions/rerun", { task_id: taskId });
    if (!res.outcome) throw new Error(res.error || "重跑失败");
    await refresh({ silent: true, keepSession: false });
    await selectAgent(res.outcome.agent_id);
    toast("已重新派发");
    return res.outcome.agent_id;
  } catch (err) {
    toastError(err, "重跑失败");
    return "";
  }
}

export async function readRunResult(agentId, taskId) {
  try {
    const res = await apiPost("console/actions/result", {
      agent_id: agentId,
      task_id: taskId,
      block: false,
      timeout_ms: 0,
    });
    toast(res.outcome?.query_status || res.outcome?.status || "已查询");
    await refresh({ silent: true, keepSession: true });
  } catch (err) {
    toastError(err, "读取结果失败");
  }
}

export async function deleteAgent(agentId) {
  try {
    await apiPost(`console/agents/${encodeURIComponent(agentId)}/delete`, {
      confirm_agent_id: agentId,
    });
    if (state.selectedAgentId === agentId) clearSelection();
    toast("Agent 及其所有 Run 已删除");
    await refresh({ silent: true, keepSession: false });
  } catch (err) {
    toastError(err, "删除 Agent 失败");
  }
}

export async function exportTranscript() {
  if (!state.selectedAgentId) return;
  try {
    await download(
      `console/agents/${encodeURIComponent(state.selectedAgentId)}/export`,
      {},
      `maid-agent-${state.selectedAgentId.slice(0, 8)}-transcript.json`,
    );
    toast("已导出");
  } catch (err) {
    toastError(err, "导出失败");
  }
}

export async function saveSettings(payload) {
  const res = await apiPost("console/settings", payload);
  state.settings = res.config || payload;
  return state.settings;
}

/* -------------------------------------------------- SSE 事件落到 store 上 */

export function applyRuntimeTrace(payload) {
  const { agent_id: agentId, task_id: taskId } = payload;
  if (!agentId || !taskId) return;

  const run = (state.runs[agentId] || []).find((item) => item.task_id === taskId);
  if (run && payload.status) run.status = payload.status;

  if (state.selectedAgentId !== agentId) return;

  const segment = (state.transcript?.runs || []).find((item) => item.task_id === taskId);
  if (segment && payload.tool_chain) segment.tool_chain = payload.tool_chain;
  // 该 run 的 trace 变了，抽屉缓存失效，下次打开重新拉。
  delete state.runTrace[taskId];
}
