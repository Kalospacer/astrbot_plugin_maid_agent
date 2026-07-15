let bridge = window.AstrBotPluginPage || null;

const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "stopping"]);
const QUIET_EVENT_TYPES = new Set(["queued", "finished"]);

const state = {
  tasks: [],
  umos: [],
  selectedUmo: "",
  selectedSessionId: "",
  sessionTasks: [],
  sessionEvents: {},
  agents: [],
  runtimeAgents: [],
  runtimeRuns: {},
  selectedAgentId: "",
  settings: null,
  overview: null,
  detail: null,
  subscriptionId: "",
  pollTimer: 0,
  refreshInFlight: false,
  refreshQueued: false,
  thinkingOpenState: {},
  renamingSessionId: "",
  pendingDeleteSessionId: "",
  pendingDeleteAgentId: "",
  pendingDeleteTimer: 0,
  pendingDeleteAgentTimer: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function compactId(value) {
  const text = String(value || "");
  return text.length > 12 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const ms = new Date(endIso) - new Date(startIso);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

function toast(message) {
  const node = $("#toast");
  if (!node) return;
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
}

function setStreamState(kind, text) {
  const node = $("#streamState");
  if (!node) return;
  node.textContent = text;
  node.className = `signal ${kind}`;
}

function ensureBridge() {
  bridge = bridge || window.AstrBotPluginPage || null;
  if (!bridge || typeof bridge.apiGet !== "function" || typeof bridge.apiPost !== "function") {
    throw new Error("AstrBot 页面桥接未加载，请从插件页面重新打开控制台。");
  }
  return bridge;
}

async function apiGet(endpoint, params) {
  return ensureBridge().apiGet(endpoint, params);
}

async function apiPost(endpoint, body) {
  return ensureBridge().apiPost(endpoint, body);
}

function isAtBottom(el) {
  if (!el) return false;
  return el.scrollHeight - el.scrollTop <= el.clientHeight + 80;
}

function scrollToBottom(el) {
  if (el) el.scrollTop = el.scrollHeight;
}

function captureFeedScroll() {
  const feed = $("#chatFeed");
  if (!feed) return null;
  return {
    wasAtBottom: isAtBottom(feed),
    bottomOffset: feed.scrollHeight - feed.scrollTop,
  };
}

function applyFeedScroll(snapshot, mode) {
  const feed = $("#chatFeed");
  if (!feed) return;
  window.setTimeout(() => {
    if (mode === "bottom") {
      scrollToBottom(feed);
      return;
    }
    if (!snapshot) return;
    if (mode === "auto" && snapshot.wasAtBottom) {
      scrollToBottom(feed);
      return;
    }
    if (mode === "preserve") {
      if (snapshot.wasAtBottom) {
        scrollToBottom(feed);
      } else {
        feed.scrollTop = Math.max(0, feed.scrollHeight - snapshot.bottomOffset);
      }
    }
  }, 50);
}

function taskTitle(task) {
  return task?.title || task?.request_text || "新会话";
}

function taskMeta(task) {
  return task?.meta && typeof task.meta === "object" ? task.meta : {};
}

function isPinned(task) {
  return Boolean(taskMeta(task).pinned);
}

function getTaskRootId(task) {
  return task?.parent_task_id || task?.task_id || "";
}

function getSelectedRootTask() {
  return state.tasks.find((task) => task.task_id === state.selectedSessionId) || null;
}

function getLatestTaskInSession() {
  return state.sessionTasks[state.sessionTasks.length - 1] || getSelectedRootTask();
}

function getActiveTaskInSession() {
  return [...state.sessionTasks].reverse().find((task) => ACTIVE_STATUSES.has(task.status)) || null;
}

function mergeTask(nextTask) {
  const existing = state.tasks.find((task) => task.task_id === nextTask.task_id);
  if (existing) {
    Object.assign(existing, nextTask);
  } else {
    state.tasks.push(nextTask);
  }
}

function updateKnownUmos() {
  const umoSet = new Set();
  for (const task of state.tasks) {
    if (task.unified_msg_origin) umoSet.add(task.unified_msg_origin);
  }
  for (const agent of state.runtimeAgents) {
    if (agent.unified_msg_origin) umoSet.add(agent.unified_msg_origin);
  }
  if (state.selectedUmo) umoSet.add(state.selectedUmo);
  state.umos = Array.from(umoSet).sort();
  if (!state.selectedUmo && state.umos.length > 0) {
    state.selectedUmo = state.umos[0];
  }
}

async function loadOverview() {
  state.overview = await apiGet("console/overview");
  state.settings = state.overview.config || state.settings;
  if (state.settings) fillSettings(state.settings);
}

async function loadAgents() {
  const data = await apiGet("console/subagents");
  state.agents = data.agents || [];
  renderAgentOptions();
}

async function loadRuntimeAgents() {
  const data = await apiGet("console/agents");
  state.runtimeAgents = data.agents || [];
  const entries = await Promise.all(
    state.runtimeAgents.map(async (agent) => {
      const detail = await apiGet(`console/agents/${encodeURIComponent(agent.agent_id)}/runs`);
      return [agent.agent_id, detail.runs || []];
    }),
  );
  state.runtimeRuns = Object.fromEntries(entries);
  updateKnownUmos();
  renderUmoSwitcher();
  updateSessionList();
}

async function loadTasks() {
  const data = await apiGet("console/tasks", { limit: 500 });
  state.tasks = data.tasks || [];
  updateKnownUmos();
  renderUmoSwitcher();
  updateSessionList();
}

async function loadSession(taskId, { scrollMode = "bottom" } = {}) {
  if (!taskId) {
    state.selectedAgentId = "";
    state.selectedSessionId = "";
    state.sessionTasks = [];
    state.sessionEvents = {};
    state.detail = null;
    renderChatFeed();
    renderInspector();
    updateSessionList();
    return;
  }

  const scrollSnapshot = captureFeedScroll();
  const selected = state.tasks.find((task) => task.task_id === taskId);
  const rootId = selected?.parent_task_id || taskId;
  state.selectedSessionId = rootId;
  state.selectedAgentId = "";
  state.sessionTasks = state.tasks
    .filter((task) => task.task_id === rootId || task.parent_task_id === rootId)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));

  state.sessionEvents = {};
  for (const task of state.sessionTasks) {
    const data = await apiGet(`console/tasks/${encodeURIComponent(task.task_id)}/events`);
    state.sessionEvents[task.task_id] = data.events || [];
  }

  const latestTask = getLatestTaskInSession();
  state.detail = latestTask
    ? await apiGet(`console/tasks/${encodeURIComponent(latestTask.task_id)}`)
    : null;

  renderChatFeed();
  renderInspector();
  updateSessionList();
  applyFeedScroll(scrollSnapshot, scrollMode);
}

async function loadRuntimeRun(agentId, taskId, { scrollMode = "bottom" } = {}) {
  const scrollSnapshot = captureFeedScroll();
  const run = (state.runtimeRuns[agentId] || []).find((item) => item.task_id === taskId);
  if (!run) return;
  let traceData = null;
  try {
    traceData = await apiGet(
      `console/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(taskId)}/trace`,
    );
  } catch {
    // The run list remains usable if a trace disappears during refresh/delete.
  }
  if (traceData?.status) run.status = traceData.status;
  if (traceData?.tool_chain) run.tool_chain = traceData.tool_chain;
  state.selectedAgentId = agentId;
  state.selectedSessionId = taskId;
  const task = {
    ...run,
    kind: "run",
    meta: {
      agent_id: run.agent_id,
      run_mode: run.mode,
      background_reason: run.background_reason,
      notification: run.notification,
      output_file: run.output_file,
      tool_chain: run.tool_chain || {},
    },
  };
  state.sessionTasks = [task];
  state.sessionEvents = {
    [taskId]: run.result || run.error
      ? [
          {
            event_type: "agent_result",
            title: run.status,
            message: run.result || run.error,
          },
        ]
      : [],
  };
  state.detail = { task, actions: [] };
  renderChatFeed();
  renderInspector();
  updateSessionList();
  applyFeedScroll(scrollSnapshot, scrollMode);
}

async function refreshConsole({ silent = false, keepSession = true } = {}) {
  if (state.refreshInFlight) {
    state.refreshQueued = true;
    return;
  }

  state.refreshInFlight = true;
  const selectedBefore = state.selectedSessionId;
  try {
    await loadTasks();
    await Promise.allSettled([loadOverview(), loadAgents(), loadRuntimeAgents()]);

    if (keepSession && selectedBefore && state.selectedAgentId) {
      const exists = (state.runtimeRuns[state.selectedAgentId] || []).some(
        (run) => run.task_id === selectedBefore,
      );
      if (exists) {
        await loadRuntimeRun(state.selectedAgentId, selectedBefore, { scrollMode: "preserve" });
      } else {
        await loadSession("");
      }
    } else if (keepSession && selectedBefore) {
      const stillExists = state.tasks.some(
        (task) => task.task_id === selectedBefore || task.parent_task_id === selectedBefore,
      );
      if (stillExists) {
        await loadSession(selectedBefore, { scrollMode: "preserve" });
      } else {
        await loadSession("");
      }
    } else if (!state.selectedSessionId) {
      renderChatFeed();
      renderInspector();
    }

    if (!silent) toast("已刷新");
  } catch (err) {
    setStreamState("signal-error", "同步异常");
    if (!silent) toast(err.message || "刷新失败");
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      window.setTimeout(() => refreshConsole({ silent: true, keepSession: true }), 200);
    }
  }
}

function startPolling() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(() => {
    refreshConsole({ silent: true, keepSession: true });
  }, 5000);
}

function renderAgentOptions() {
  const select = $("#dispatchAgent");
  if (!select) return;
  const configuredDefault =
    state.settings?.default_agent_name || state.overview?.config?.default_agent_name || "butler";
  const names = [...new Set([configuredDefault, ...state.agents.map((agent) => agent.name)].filter(Boolean))];
  select.innerHTML = names
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join("");
}

function renderUmoSwitcher() {
  const nameNode = $("#currentUmoName");
  if (nameNode) nameNode.textContent = state.selectedUmo || "选择 UMO";

  const list = $("#umoListContainer");
  if (!list) return;
  if (state.umos.length === 0) {
    list.innerHTML = `<div class="modal-empty">没有历史来源。手动输入一个 AstrBot UMO。</div>`;
    return;
  }
  list.innerHTML = state.umos
    .map(
      (umo) => `
        <div class="umo-list-item ${umo === state.selectedUmo ? "active" : ""}" data-umo="${escapeHtml(umo)}">
          <div class="umo-avatar">${escapeHtml(umo.slice(0, 2).toUpperCase())}</div>
          <div class="umo-name">${escapeHtml(umo)}</div>
        </div>
      `,
    )
    .join("");
}

function updateSessionList() {
  const list = $("#sessionList");
  if (!list) return;

  const runtimeAgents = state.runtimeAgents
    .filter((agent) => agent.unified_msg_origin === state.selectedUmo)
    .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
  const runtimeTaskIds = new Set(
    Object.values(state.runtimeRuns)
      .flat()
      .map((run) => run.task_id),
  );
  const rootTasks = state.tasks
    .filter(
      (task) =>
        task.unified_msg_origin === state.selectedUmo &&
        !task.parent_task_id &&
        !runtimeTaskIds.has(task.task_id),
    )
    .sort((a, b) => {
      const pinnedDelta = Number(isPinned(b)) - Number(isPinned(a));
      if (pinnedDelta !== 0) return pinnedDelta;
      return new Date(b.updated_at) - new Date(a.updated_at);
    });

  if (runtimeAgents.length === 0 && rootTasks.length === 0) {
    list.innerHTML = `<div class="empty-state">暂无 Agent / Run</div>`;
    return;
  }

  const runtimeHtml = runtimeAgents
    .map((agent) => {
      const runs = [...(state.runtimeRuns[agent.agent_id] || [])].sort(
        (a, b) => new Date(b.created_at) - new Date(a.created_at),
      );
      const hasActiveRun = runs.some((run) => ACTIVE_STATUSES.has(run.status));
      const hasPendingNotification = runs.some(
        (run) => run.notification && !run.notification.delivered,
      );
      const deletingAgent = agent.agent_id === state.pendingDeleteAgentId;
      const runsHtml = runs
        .map(
          (run) => `
            <div class="session-item runtime-session-item ${run.task_id === state.selectedSessionId ? "active" : ""} ${deletingAgent ? "delete-armed" : ""}"
                 data-id="${escapeHtml(run.task_id)}" data-runtime-agent="${escapeHtml(agent.agent_id)}">
              <div class="session-copy">
                <div class="session-title">↳ ${escapeHtml(run.request_text || compactId(run.task_id))}</div>
                <small>${escapeHtml(run.mode)} · ${escapeHtml(run.status)}${run.notification && !run.notification.delivered ? " · 待通知" : ""}</small>
              </div>
              <div class="session-actions">
                ${ACTIVE_STATUSES.has(run.status) ? `
                  <button class="session-action-btn" type="button" data-runtime-action="stop" aria-label="停止" title="停止此 Run">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="1"></rect></svg>
                  </button>
                ` : ""}
                <button class="session-action-btn" type="button" data-runtime-action="result" aria-label="读取结果" title="读取此 Run 的结果">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"></path><path d="M8 9h8M8 13h6M8 17h4"></path></svg>
                </button>
                <button class="session-action-btn delete-btn" type="button" data-runtime-action="delete-agent" aria-label="删除 Agent" title="${hasActiveRun ? "Agent 仍在运行，无法删除" : hasPendingNotification ? "请先读取待通知结果" : deletingAgent ? "再次点击确认删除整个 Agent" : "删除整个 Agent（含所有 Run）"}" ${hasActiveRun || hasPendingNotification ? "disabled" : ""}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>
              </div>
            </div>
          `,
        )
        .join("");
      return `
        <div class="session-group-title">Agent ${escapeHtml(compactId(agent.agent_id))} · ${escapeHtml(agent.agent_name)}</div>
        ${runsHtml || '<div class="empty-state">暂无 Run</div>'}
      `;
    })
    .join("");

  list.innerHTML =
    runtimeHtml +
    (rootTasks.length ? `<div class="session-group-title">SQLite 审计记录</div>` : "") +
    rootTasks
      .map((task) => {
        const active = task.task_id === state.selectedSessionId ? "active" : "";
        const pinned = isPinned(task) ? "pinned" : "";
        const deleting = task.task_id === state.pendingDeleteSessionId ? "delete-armed" : "";
        const isRenaming = task.task_id === state.renamingSessionId;
        const titleHtml = isRenaming
          ? `
            <form class="session-rename-form" data-id="${escapeHtml(task.task_id)}">
              <input class="session-rename-input" value="${escapeHtml(taskTitle(task))}" maxlength="120" autocomplete="off" />
            </form>
          `
          : `<div class="session-title">${escapeHtml(taskTitle(task))}</div>`;
        return `
          <div class="session-item ${active} ${pinned} ${deleting}" data-id="${escapeHtml(task.task_id)}">
            ${titleHtml}
            <div class="session-actions">
              <button class="session-action-btn pin-btn" type="button" data-session-action="pin" aria-label="置顶" title="${isPinned(task) ? "取消置顶" : "置顶"}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 4 5 5-4 4v5l-2 2-5-5-4 4-1-1 4-4-5-5 2-2h5l4-4Z"></path></svg>
              </button>
              <button class="session-action-btn rename-btn" type="button" data-session-action="rename" aria-label="重命名" title="重命名">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>
              </button>
              <button class="session-action-btn delete-btn" type="button" data-session-action="delete" aria-label="删除" title="${deleting ? "再次点击确认删除" : "删除"}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
              </button>
            </div>
          </div>
        `;
      })
      .join("");
}

function captureThinkingOpenState() {
  $$("details[data-thinking-key]").forEach((details) => {
    state.thinkingOpenState[details.dataset.thinkingKey] = details.open;
  });
}

function getThinkingKey(task) {
  return `thinking:${task.task_id}`;
}

function getThinkingOpenAttribute(task, defaultOpen) {
  const key = getThinkingKey(task);
  const hasStoredState = Object.prototype.hasOwnProperty.call(state.thinkingOpenState, key);
  const shouldOpen = hasStoredState ? state.thinkingOpenState[key] : defaultOpen;
  return shouldOpen ? "open" : "";
}

function renderChatFeed() {
  const feed = $("#chatFeed");
  if (!feed) return;

  if (!state.selectedSessionId) {
    feed.classList.add("is-empty");
    feed.innerHTML = `
      <div class="empty-hero" id="emptyHero">
        <div class="brand-logo">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10H12V2Z"></path><path d="M12 12 2.1 7.1"></path><path d="m12 12 9.9 4.9"></path></svg>
        </div>
      </div>
    `;
    $("#promptText")?.focus();
    return;
  }

  captureThinkingOpenState();
  feed.classList.remove("is-empty");
  let html = "";

  for (const task of state.sessionTasks) {
    if (task.request_text) {
      html += `
        <div class="chat-message user">
          <div class="chat-message-inner">
            <div class="bubble">${escapeHtml(task.request_text)}</div>
          </div>
        </div>
      `;
    }

    const events = state.sessionEvents[task.task_id] || [];
    let eventLinesHtml = "";
    let responseHtml = "";

    for (const event of events) {
      // agent_output 是流式过程快照（每步一条、含 tool_call 文本化），
      // 过程由 trace 面板表达，不进回复气泡——否则会复述工具调用且与 agent_result 撞车重复。
      if (event.event_type === "agent_output") {
        continue;
      }
      // 回复气泡只认最终结果与工具主动发送的消息。
      if (event.event_type === "agent_result" || event.event_type === "tool_direct_message") {
        if (event.message) responseHtml += `${escapeHtml(event.message)}\n\n`;
        continue;
      }
      if (QUIET_EVENT_TYPES.has(event.event_type) || event.event_type?.startsWith("system")) {
        continue;
      }
      eventLinesHtml += `<div class="trace-event-line"><strong>${escapeHtml(event.title || event.event_type)}</strong>${event.message ? ` - ${escapeHtml(event.message)}` : ""}</div>`;
    }

    const traceHtml = renderTracePanel(task, "feed", eventLinesHtml);
    if (traceHtml || responseHtml || ["error", "failed", "stopped"].includes(task.status)) {
      html += `
        <div class="chat-message maid">
          <div class="chat-message-inner">
            <div class="chat-avatar">M</div>
            <div class="assistant-flow">
      `;

      html += traceHtml;

      if (responseHtml) {
        html += `<div class="bubble">${responseHtml.trim()}</div>`;
      } else if (["error", "failed", "stopped"].includes(task.status)) {
        html += `<div class="bubble error-text">任务发生异常，已终止。</div>`;
      }

      html += `
            </div>
          </div>
        </div>
      `;
    }
  }

  feed.innerHTML = html;
}

function stringifyStructuredValue(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const TRACE_ERROR_PATTERN = /^\s*(error|exception|traceback|失败|错误|超时)/i;

function clipTraceValue(text, max) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  if (value.length <= max) return value;
  if (/[\\/]/.test(value)) return `…${value.slice(-(max - 1))}`;
  return `${value.slice(0, max - 1)}…`;
}

function summarizeToolArgs(args) {
  if (args === undefined || args === null) return "";
  if (typeof args !== "object" || Array.isArray(args)) {
    return clipTraceValue(stringifyStructuredValue(args), 60);
  }
  const keys = Object.keys(args);
  const parts = [];
  for (const key of keys) {
    const value = args[key];
    const isText = typeof value === "string";
    const shown = clipTraceValue(isText ? value : stringifyStructuredValue(value), 40);
    parts.push(isText ? `${key}: "${shown}"` : `${key}: ${shown}`);
    if (parts.length >= 2) break;
  }
  const extra = keys.length - parts.length;
  return extra > 0 ? `${parts.join(", ")}, +${extra}` : parts.join(", ");
}

function pairToolChainSteps(entries) {
  const steps = [];
  const byCallId = new Map();

  for (const raw of entries) {
    const entry = raw && typeof raw === "object" ? raw : {};
    const kind = entry.kind || "";

    if (kind === "tool_call") {
      const step = {
        type: "tool",
        name: String(entry.tool_name || ""),
        callId: String(entry.tool_call_id || ""),
        argsText: stringifyStructuredValue(entry.arguments ?? entry.message),
        argsSummary: summarizeToolArgs(entry.arguments),
        result: null,
        index: entry.index,
      };
      steps.push(step);
      if (step.callId) {
        if (!byCallId.has(step.callId)) byCallId.set(step.callId, []);
        byCallId.get(step.callId).push(step);
      }
      continue;
    }

    if (kind === "tool_result") {
      const callId = String(entry.tool_call_id || "");
      const text = String(entry.message ?? "");
      const siblings = callId ? byCallId.get(callId) || [] : [];
      const pending = siblings.find((step) => step.result === null);
      if (pending) {
        pending.result = text;
        continue;
      }
      const latest = siblings[siblings.length - 1];
      if (latest) {
        latest.result = `${latest.result ?? ""}\n${text}`.trim();
        continue;
      }
      steps.push({
        type: "tool",
        name: String(entry.tool_name || ""),
        callId,
        argsText: "",
        argsSummary: "",
        result: text,
        index: entry.index,
        orphan: true,
      });
      continue;
    }

    if (kind === "assistant") {
      const text = String(entry.message ?? "").trim();
      if (text) steps.push({ type: "text", text, index: entry.index });
    }
  }

  return steps;
}

function traceStepStatus(step, taskActive) {
  if (step.result !== null && step.result !== undefined) {
    return TRACE_ERROR_PATTERN.test(step.result) ? "error" : "ok";
  }
  return taskActive ? "running" : "dead";
}

function truncateTraceText(text, { lines = 3, chars = 200 } = {}) {
  const value = String(text ?? "").trim();
  if (!value) return { preview: "", truncated: false, remainLines: 0 };
  const allLines = value.split("\n");
  let preview = allLines.slice(0, lines).join("\n");
  let truncated = allLines.length > lines;
  if (preview.length > chars) {
    preview = preview.slice(0, chars);
    truncated = true;
  }
  return { preview, truncated, remainLines: Math.max(0, allLines.length - lines) };
}

function getStepOpenAttribute(stepKey) {
  return state.thinkingOpenState[stepKey] ? "open" : "";
}

function renderTraceExpandHint(remainLines) {
  const label = remainLines > 0 ? `+${remainLines} 行` : "展开";
  return `<span class="trace-expand-hint">(${escapeHtml(label)})</span>`;
}

function renderTraceStep(step, taskActive, stepKey) {
  if (step.type === "text") {
    const { preview, truncated, remainLines } = truncateTraceText(step.text, { lines: 3, chars: 300 });
    const headLine = (bodyHtml) => `
      <span class="trace-head-line">
        <span class="trace-dot trace-dot-text"></span>
        <span class="trace-text-body">${bodyHtml}</span>
      </span>
    `;
    if (!truncated) {
      return `<div class="trace-step trace-step-text">${headLine(escapeHtml(preview))}</div>`;
    }
    return `
      <details class="trace-step trace-step-text" data-thinking-key="${escapeHtml(stepKey)}" ${getStepOpenAttribute(stepKey)}>
        <summary class="trace-step-head">
          ${headLine(`${escapeHtml(preview)}… ${renderTraceExpandHint(remainLines)}`)}
        </summary>
        <div class="trace-step-detail">
          <pre class="trace-detail-pre">${escapeHtml(step.text)}</pre>
        </div>
      </details>
    `;
  }

  const status = traceStepStatus(step, taskActive);
  const name = step.name || (step.orphan ? compactId(step.callId) : "") || "工具";
  const argsSummary = step.argsSummary ? `(${escapeHtml(step.argsSummary)})` : "()";

  let previewHtml = "";
  if (step.result !== null && step.result !== undefined && String(step.result).trim()) {
    const { preview, truncated, remainLines } = truncateTraceText(step.result);
    previewHtml = `
      <span class="trace-result-preview">${escapeHtml(preview)}${truncated ? `… ${renderTraceExpandHint(remainLines)}` : ""}</span>
    `;
  }

  const detailSections = [];
  if (step.argsText) {
    detailSections.push(`
      <span class="trace-detail-label">参数</span>
      <pre class="trace-detail-pre">${escapeHtml(step.argsText)}</pre>
    `);
  }
  if (step.result !== null && step.result !== undefined && String(step.result).trim()) {
    detailSections.push(`
      <span class="trace-detail-label">结果</span>
      <pre class="trace-detail-pre">${escapeHtml(String(step.result))}</pre>
    `);
  }
  const detailHtml = detailSections.length
    ? `<div class="trace-step-detail">${detailSections.join("")}</div>`
    : "";

  return `
    <details class="trace-step trace-step-tool" data-thinking-key="${escapeHtml(stepKey)}" ${getStepOpenAttribute(stepKey)}>
      <summary class="trace-step-head">
        <span class="trace-head-line">
          <span class="trace-dot trace-dot-${status}"></span>
          <span class="trace-step-main">
            <span class="trace-tool-name">${escapeHtml(name)}</span><span class="trace-tool-args">${argsSummary}</span>
            ${previewHtml}
          </span>
        </span>
      </summary>
      ${detailHtml}
    </details>
  `;
}

function renderTracePanel(task, variant, extraHtml = "") {
  if (!task) return "";
  const chain = taskMeta(task).tool_chain || {};
  const entries = Array.isArray(chain.entries) ? chain.entries : [];
  const steps = pairToolChainSteps(entries);
  const isActive = ACTIVE_STATUSES.has(task.status);

  if (steps.length === 0 && !isActive && !extraHtml) return "";

  const stepsHtml = steps
    .map((step, position) => {
      const stepKey = `step:${task.task_id}:${step.callId || `i${step.index ?? position}`}`;
      return renderTraceStep(step, isActive, stepKey);
    })
    .join("");

  const runningHtml = isActive
    ? `<div class="trace-running-line"><span class="trace-dot trace-dot-running"></span>运行中…</div>`
    : "";
  const eventsHtml = extraHtml ? `<div class="trace-events">${extraHtml}</div>` : "";
  const body = `<div class="trace-body">${stepsHtml}${runningHtml}${eventsHtml}</div>`;

  if (variant === "inspector") {
    return `<div class="trace-panel trace-panel-inspector">${body}</div>`;
  }

  const toolCount = steps.filter((step) => step.type === "tool").length;
  const duration = formatDuration(task.created_at, task.completed_at || task.updated_at);
  const summaryText = [
    `${toolCount} 次工具调用`,
    duration,
    isActive ? "运行中" : task.status,
  ]
    .filter(Boolean)
    .join(" · ");

  return `
    <details class="trace-panel" data-thinking-key="${escapeHtml(getThinkingKey(task))}" ${getThinkingOpenAttribute(task, isActive)}>
      <summary class="trace-summary">
        <span class="trace-caret">▸</span>
        <span>${escapeHtml(summaryText)}</span>
      </summary>
      ${body}
    </details>
  `;
}

function renderToolChain(task) {
  const node = $("#toolChain");
  if (!node) return;
  const chain = task ? taskMeta(task).tool_chain || {} : {};
  const rawMessages = Array.isArray(chain.messages) ? chain.messages : [];
  const panelHtml = task ? renderTracePanel(task, "inspector") : "";

  if (!panelHtml && rawMessages.length === 0) {
    node.innerHTML = `<div class="empty-state">暂无工具调用链</div>`;
    return;
  }

  const rawHtml =
    rawMessages.length > 0
      ? `
        <details class="tool-chain-raw">
          <summary>原始 messages (${escapeHtml(rawMessages.length)})</summary>
          <pre class="raw-box">${escapeHtml(JSON.stringify(rawMessages, null, 2))}</pre>
        </details>
      `
      : "";

  node.innerHTML = `${panelHtml}${rawHtml}`;
}

function renderInspector() {
  const task = state.detail?.task;
  if (!task) {
    $("#inspectorTitle").textContent = "未选择";
    $("#taskFacts").innerHTML = "";
    $("#actionList").innerHTML = "";
    $("#rawRequest").textContent = "";
    $("#rawMeta").textContent = "";
    renderToolChain(null);
    return;
  }

  $("#inspectorTitle").textContent = compactId(task.task_id);
  $("#taskFacts").innerHTML = `
    <dt>状态</dt><dd>${escapeHtml(task.status)}</dd>
    <dt>类型</dt><dd>${escapeHtml(task.kind)}</dd>
    <dt>Agent</dt><dd>${escapeHtml(task.agent_name)}</dd>
    <dt>Agent ID</dt><dd>${escapeHtml(compactId(task.agent_id || taskMeta(task).agent_id))}</dd>
    <dt>模式</dt><dd>${escapeHtml(task.mode || taskMeta(task).run_mode || "")}</dd>
    <dt>通知</dt><dd>${escapeHtml(task.notification?.notification_id || taskMeta(task).notification?.notification_id || "-")}</dd>
    <dt>来源 UMO</dt><dd>${escapeHtml(task.unified_msg_origin)}</dd>
    <dt>更新时间</dt><dd>${escapeHtml(formatTime(task.updated_at))}</dd>
  `;
  if ($("#stopButton")) {
    $("#stopButton").disabled = !["starting", "running"].includes(task.status);
  }

  $("#actionList").innerHTML = (state.detail.actions || [])
    .map((action) => {
      const cls = action.status === "error" ? 'style="color:var(--accent-error);"' : "";
      return `
        <div class="action-item" ${cls}>
          <strong>${escapeHtml(action.action)}</strong>
          <small>${escapeHtml(formatTime(action.created_at))}</small>
          ${action.result_text ? `<div class="action-result">${escapeHtml(action.result_text)}</div>` : ""}
        </div>
      `;
    })
    .join("");

  $("#rawRequest").textContent = task.request_text || "";
  $("#rawMeta").textContent = JSON.stringify(taskMeta(task), null, 2);
  renderToolChain(task);
}

async function submitPrompt() {
  const input = $("#promptText");
  const text = input?.value.trim() || "";
  if (!text) return;

  const agent = $("#dispatchAgent")?.value || state.settings?.default_agent_name || "butler";
  const selectedRoot = getSelectedRootTask();
  const umo = selectedRoot?.unified_msg_origin || state.selectedUmo;
  if (!umo) {
    toast("先选择或填写 UMO");
    $("#umoModal")?.classList.remove("hidden");
    return;
  }

  input.value = "";
  input.style.height = "auto";
  const btn = $("#sendBtn");
  if (btn) btn.disabled = true;

  try {
    const runtimeRun = state.selectedAgentId
      ? (state.runtimeRuns[state.selectedAgentId] || []).find(
          (run) => run.task_id === state.selectedSessionId,
        )
      : null;
    if (runtimeRun && ["starting", "running"].includes(runtimeRun.status)) {
      await apiPost("console/actions/steer", {
        agent_id: state.selectedAgentId,
        task_id: runtimeRun.task_id,
        message_text: text,
      });
      toast("已 steer 当前 Agent");
      await refreshConsole({ silent: true, keepSession: true });
      return;
    }
    if (runtimeRun && state.selectedAgentId) {
      const res = await apiPost("console/actions/resume", {
        agent_id: state.selectedAgentId,
        request_text: text,
        unified_msg_origin: umo,
      });
      if (!res.outcome) throw new Error(res.error || "resume 失败");
      await refreshConsole({ silent: true, keepSession: false });
      await loadRuntimeRun(res.outcome.agent_id, res.outcome.task_id, { scrollMode: "bottom" });
      toast("已 Resume Agent");
      return;
    }

    const activeTask = getActiveTaskInSession();
    let res;
    if (activeTask) {
      res = await apiPost("console/actions/steer", {
        task_id: activeTask.task_id,
        message_text: text,
      });
      toast("已续接当前任务");
      await refreshConsole({ silent: true, keepSession: true });
      return;
    }

    res = await apiPost("console/actions/dispatch", {
      unified_msg_origin: umo,
      agent_name: agent,
      request_text: text,
      run_in_background: true,
    });
    if (!res.outcome) throw new Error(res.error || "派发失败");
    await refreshConsole({ silent: true, keepSession: false });
    await loadRuntimeRun(res.outcome.agent_id, res.outcome.task_id, { scrollMode: "bottom" });
    toast("已创建 Agent Run");
  } catch (err) {
    toast(err.message || "发送失败");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteSession(taskId) {
  try {
    await apiPost(`console/tasks/${encodeURIComponent(taskId)}/delete`, {});
    toast("已删除");
    await loadSession("");
    await refreshConsole({ silent: true, keepSession: false });
  } catch (err) {
    toast(err.message || "删除失败");
  }
}

async function stopRuntimeRun(taskId) {
  try {
    await apiPost("console/actions/stop", { task_id: taskId });
    toast("已请求停止");
    await refreshConsole({ silent: true, keepSession: true });
  } catch (err) {
    toast(err.message || "停止失败");
  }
}

async function readRuntimeResult(agentId, taskId) {
  try {
    const res = await apiPost("console/actions/result", {
      agent_id: agentId,
      task_id: taskId,
      block: false,
      timeout_ms: 0,
    });
    toast(res.outcome?.query_status || res.outcome?.status || "已查询");
    await refreshConsole({ silent: true, keepSession: true });
  } catch (err) {
    toast(err.message || "读取结果失败");
  }
}

async function deleteRuntimeAgent(agentId) {
  try {
    await apiPost(`console/agents/${encodeURIComponent(agentId)}/delete`, {
      confirm_agent_id: agentId,
    });
    state.pendingDeleteAgentId = "";
    if (state.selectedAgentId === agentId) await loadSession("");
    toast("Agent 及其所有 Run 已删除");
    await refreshConsole({ silent: true, keepSession: false });
  } catch (err) {
    toast(err.message || "删除 Agent 失败");
  }
}

function requestDeleteRuntimeAgent(agentId) {
  const runs = state.runtimeRuns[agentId] || [];
  if (runs.some((run) => ACTIVE_STATUSES.has(run.status))) {
    toast("Agent 仍有活跃 Run，请先停止");
    return;
  }
  if (runs.some((run) => run.notification && !run.notification.delivered)) {
    toast("Agent 仍有待通知结果，请先读取结果");
    return;
  }
  if (state.pendingDeleteAgentId !== agentId) {
    state.pendingDeleteAgentId = agentId;
    window.clearTimeout(state.pendingDeleteAgentTimer);
    state.pendingDeleteAgentTimer = window.setTimeout(() => {
      if (state.pendingDeleteAgentId === agentId) {
        state.pendingDeleteAgentId = "";
        updateSessionList();
      }
    }, 3200);
    updateSessionList();
    toast("再次点击删除整个 Agent 及其所有 Run");
    return;
  }
  window.clearTimeout(state.pendingDeleteAgentTimer);
  state.pendingDeleteAgentId = "";
  deleteRuntimeAgent(agentId);
}

function requestDeleteSession(taskId) {
  if (state.pendingDeleteSessionId !== taskId) {
    state.pendingDeleteSessionId = taskId;
    window.clearTimeout(state.pendingDeleteTimer);
    state.pendingDeleteTimer = window.setTimeout(() => {
      if (state.pendingDeleteSessionId === taskId) {
        state.pendingDeleteSessionId = "";
        updateSessionList();
      }
    }, 3200);
    updateSessionList();
    toast("再次点击删除此对话");
    return;
  }
  window.clearTimeout(state.pendingDeleteTimer);
  state.pendingDeleteSessionId = "";
  deleteSession(taskId);
}

function startRenameSession(taskId) {
  state.renamingSessionId = taskId;
  updateSessionList();
  window.setTimeout(() => {
    const input = $(".session-rename-input");
    if (input) {
      input.focus();
      input.select();
    }
  }, 0);
}

async function renameSession(taskId, nextTitle) {
  const task = state.tasks.find((item) => item.task_id === taskId);
  const title = String(nextTitle || "").trim();
  state.renamingSessionId = "";
  if (!title || title === taskTitle(task)) {
    updateSessionList();
    return;
  }
  try {
    const res = await apiPost(`console/tasks/${encodeURIComponent(taskId)}/update`, { title });
    if (res.task) mergeTask(res.task);
    updateSessionList();
    renderInspector();
    toast("已重命名");
  } catch (err) {
    toast(err.message || "重命名失败");
  }
}

async function togglePinSession(taskId) {
  const task = state.tasks.find((item) => item.task_id === taskId);
  if (!task) return;
  try {
    const res = await apiPost(`console/tasks/${encodeURIComponent(taskId)}/update`, {
      meta: { pinned: !isPinned(task) },
    });
    if (res.task) mergeTask(res.task);
    updateSessionList();
    toast(isPinned(res.task || task) ? "已置顶" : "已取消置顶");
  } catch (err) {
    toast(err.message || "置顶失败");
  }
}

async function stopCurrentTask() {
  const task = state.detail?.task;
  if (!task) return;
  try {
    await apiPost("console/actions/stop", { task_id: task.task_id });
    toast("已请求停止");
    await refreshConsole({ silent: true, keepSession: true });
  } catch (err) {
    toast(err.message || "停止失败");
  }
}

async function readCurrentResult() {
  const task = state.detail?.task;
  if (!task) return;
  try {
    const res = await apiPost("console/actions/result", {
      task_id: task.task_id,
      agent_id: task.agent_id || taskMeta(task).agent_id || "",
      block: false,
      timeout_ms: 0,
    });
    toast(res.outcome?.query_status || res.outcome?.status || "已查询");
    await refreshConsole({ silent: true, keepSession: true });
  } catch (err) {
    toast(err.message || "读取结果失败");
  }
}

async function rerunCurrentTask() {
  const task = state.detail?.task;
  if (!task) return;
  try {
    const res = await apiPost("console/actions/rerun", { task_id: task.task_id });
    if (!res.outcome) throw new Error(res.error || "重跑失败");
    await refreshConsole({ silent: true, keepSession: false });
    await loadRuntimeRun(res.outcome.agent_id, res.outcome.task_id, { scrollMode: "bottom" });
    toast("已重新派发");
  } catch (err) {
    toast(err.message || "重跑失败");
  }
}

async function exportHistory() {
  try {
    await ensureBridge().download("console/export", {}, `maid-history-${Date.now()}.json`);
    toast("已导出");
  } catch (err) {
    toast(err.message || "导出失败");
  }
}

function parseSsePayload(event) {
  if (event?.parsed && typeof event.parsed === "object") return event.parsed;
  const raw = typeof event === "string" ? event : event?.raw || event?.data || "";
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

window.handleSseMessage = async function handleSseMessage(event) {
  const data = parseSsePayload(event);
  if (!data) return;

  if (data.type === "closed") {
    setStreamState("signal-poll", "轮询同步");
    return;
  }

  if (data.type === "reset") {
    await refreshConsole({ silent: true, keepSession: true });
    return;
  }

  if (data.type === "runtime_trace" && data.agent_id && data.task_id) {
    const run = (state.runtimeRuns[data.agent_id] || []).find(
      (item) => item.task_id === data.task_id,
    );
    if (run) {
      if (data.status) run.status = data.status;
      run.tool_chain = data.tool_chain || {};
    }
    if (
      state.selectedAgentId === data.agent_id &&
      state.selectedSessionId === data.task_id
    ) {
      const task = state.sessionTasks.find((item) => item.task_id === data.task_id);
      if (task) {
        if (data.status) task.status = data.status;
        task.meta = { ...task.meta, tool_chain: data.tool_chain || {} };
        if (state.detail?.task?.task_id === data.task_id) state.detail.task = task;
      }
      const feed = $("#chatFeed");
      const autoScroll = isAtBottom(feed);
      renderChatFeed();
      if (autoScroll) scrollToBottom(feed);
      renderInspector();
      updateSessionList();
    }
    return;
  }

  if (data.type === "task" && data.task) {
    const alreadyInSession = state.sessionTasks.some(
      (task) => task.task_id === data.task.task_id,
    );
    mergeTask(data.task);
    updateKnownUmos();
    renderUmoSwitcher();
    updateSessionList();
    const belongsToSession =
      state.selectedSessionId &&
      (data.task.task_id === state.selectedSessionId ||
        data.task.parent_task_id === state.selectedSessionId);
    if (!belongsToSession) return;

    if (alreadyInSession) {
      // mergeTask 更新的是同一对象引用，sessionTasks 已是最新，
      // meta.tool_chain / status 的变化只需本地重绘，避免每个 step 全量重拉 events。
      if (state.detail?.task?.task_id === data.task.task_id) {
        Object.assign(state.detail.task, data.task);
      }
      const feed = $("#chatFeed");
      const autoScroll = isAtBottom(feed);
      renderChatFeed();
      if (autoScroll) scrollToBottom(feed);
      renderInspector();
      return;
    }

    await loadSession(state.selectedSessionId, { scrollMode: "preserve" });
    return;
  }

  if (data.type === "event" && data.event) {
    const eventTask = state.tasks.find((task) => task.task_id === data.event.task_id);
    const rootId = getTaskRootId(eventTask);
    if (rootId === state.selectedSessionId) {
      if (!state.sessionEvents[data.event.task_id]) state.sessionEvents[data.event.task_id] = [];
      state.sessionEvents[data.event.task_id].push(data.event);
      const feed = $("#chatFeed");
      const autoScroll = isAtBottom(feed);
      renderChatFeed();
      if (autoScroll) scrollToBottom(feed);
      renderInspector();
    }
    return;
  }

  if (data.type === "action" && data.action && state.detail?.task?.task_id === data.action.task_id) {
    state.detail.actions = state.detail.actions || [];
    state.detail.actions.push(data.action);
    renderInspector();
  }
};

async function subscribeStream() {
  if (!bridge || typeof bridge.subscribeSSE !== "function") {
    setStreamState("signal-poll", "轮询同步");
    return;
  }

  try {
    state.subscriptionId = await bridge.subscribeSSE("console/stream", {
      onOpen() {
        setStreamState("signal-active", "实时同步");
      },
      onMessage(event) {
        window.handleSseMessage(event);
      },
      onError() {
        setStreamState("signal-poll", "轮询同步");
      },
    });
  } catch (err) {
    setStreamState("signal-poll", "轮询同步");
  }
}

function fillSettings(config) {
  $("#settingDefaultAgent") && ($("#settingDefaultAgent").value = config.default_agent_name || "");
  $("#settingAllowedAgents") &&
    ($("#settingAllowedAgents").value = (config.allowed_agent_names || []).join(", "));
  $("#settingHideNative") && ($("#settingHideNative").checked = Boolean(config.hide_native_tools));
  $("#settingHideTransfer") &&
    ($("#settingHideTransfer").checked = Boolean(config.hide_transfer_tools));
  $("#settingRawInput") &&
    ($("#settingRawInput").checked = Boolean(config.include_raw_user_input));
  $("#settingLogRaw") && ($("#settingLogRaw").checked = Boolean(config.log_raw_llm_io));
  $("#settingForegroundTimeout") &&
    ($("#settingForegroundTimeout").value = config.foreground_timeout_seconds ?? 50);
  $("#settingMemoryAgents") &&
    ($("#settingMemoryAgents").value = (config.memory_agent_names || []).join(", "));
  $("#settingMaxPerUmo") &&
    ($("#settingMaxPerUmo").value = config.max_active_per_umo ?? 5);
  $("#settingMaxGlobal") &&
    ($("#settingMaxGlobal").value = config.max_active_global ?? 20);
  $("#settingRetention") &&
    ($("#settingRetention").value = config.retention_days ?? 30);
  $("#settingPrompt") &&
    ($("#settingPrompt").value = config.dispatch_prompt_template || "");
}

function bindEvents() {
  $("#refreshButton")?.addEventListener("click", () => {
    refreshConsole({ silent: false, keepSession: true });
  });

  $("#newChatButton")?.addEventListener("click", () => {
    loadSession("");
  });

  const sessionList = $("#sessionList");
  sessionList?.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const item = target.closest(".session-item");
    if (!item) return;
    const taskId = item.dataset.id;
    const runtimeAgentId = item.dataset.runtimeAgent;
    const actionButton = target.closest("[data-session-action], [data-runtime-action]");
    if (!actionButton && target.closest(".session-rename-form")) return;

    if (actionButton) {
      event.stopPropagation();
      const sessionAction = actionButton.dataset.sessionAction;
      const runtimeAction = actionButton.dataset.runtimeAction;
      if (sessionAction === "delete") requestDeleteSession(taskId);
      if (sessionAction === "rename") startRenameSession(taskId);
      if (sessionAction === "pin") await togglePinSession(taskId);
      if (runtimeAction === "stop") await stopRuntimeRun(taskId);
      if (runtimeAction === "result") await readRuntimeResult(runtimeAgentId, taskId);
      if (runtimeAction === "delete-agent") requestDeleteRuntimeAgent(runtimeAgentId);
      return;
    }

    if (runtimeAgentId) {
      await loadRuntimeRun(runtimeAgentId, taskId);
    } else {
      await loadSession(taskId);
    }
  });

  sessionList?.addEventListener("submit", async (event) => {
    const form = event.target instanceof Element ? event.target.closest(".session-rename-form") : null;
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector(".session-rename-input");
    await renameSession(form.dataset.id, input?.value || "");
  });

  sessionList?.addEventListener("focusout", async (event) => {
    const input = event.target instanceof Element ? event.target.closest(".session-rename-input") : null;
    if (!input) return;
    const form = input.closest(".session-rename-form");
    if (!form || state.renamingSessionId !== form.dataset.id) return;
    await renameSession(form.dataset.id, input.value);
  });

  sessionList?.addEventListener("keydown", (event) => {
    const input = event.target instanceof Element ? event.target.closest(".session-rename-input") : null;
    if (!input) return;
    if (event.key === "Escape") {
      event.preventDefault();
      state.renamingSessionId = "";
      updateSessionList();
    }
  });

  $("#umoSwitcher")?.addEventListener("click", () => {
    $("#umoModal")?.classList.remove("hidden");
  });

  $("#umoListContainer")?.addEventListener("click", (event) => {
    const item = event.target.closest(".umo-list-item");
    if (!item) return;
    state.selectedUmo = item.dataset.umo;
    $("#umoModal")?.classList.add("hidden");
    renderUmoSwitcher();
    updateSessionList();
    loadSession("");
  });

  $("#newUmoForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#newUmoInput");
    const value = input?.value.trim() || "";
    if (!value) return;
    if (!state.umos.includes(value)) {
      state.umos.push(value);
      state.umos.sort();
    }
    state.selectedUmo = value;
    input.value = "";
    $("#umoModal")?.classList.add("hidden");
    renderUmoSwitcher();
    updateSessionList();
    loadSession("");
  });

  const promptText = $("#promptText");
  promptText?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitPrompt();
    }
  });
  promptText?.addEventListener("input", function resizeInput() {
    this.style.height = "auto";
    this.style.height = `${this.scrollHeight}px`;
  });

  $("#chatForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitPrompt();
  });

  $("#chatFeed")?.addEventListener(
    "toggle",
    (event) => {
      const details = event.target;
      if (!(details instanceof HTMLDetailsElement)) return;
      const key = details.dataset.thinkingKey;
      if (key) state.thinkingOpenState[key] = details.open;
    },
    true,
  );

  $("#toggleRight")?.addEventListener("click", () => {
    $("#paneRight")?.classList.toggle("collapsed");
  });

  $$(".tab").forEach((button) => {
    button.addEventListener("click", (event) => {
      const target = event.currentTarget;
      $$(".tab").forEach((item) => item.classList.remove("active"));
      $$(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      target.classList.add("active");
      $(`#${target.dataset.tab}Tab`)?.classList.add("active");
    });
  });

  $("#stopButton")?.addEventListener("click", stopCurrentTask);
  $("#rerunButton")?.addEventListener("click", rerunCurrentTask);
  $("#resultButton")?.addEventListener("click", readCurrentResult);
  $("#exportButton")?.addEventListener("click", exportHistory);

  $$(".close-modal").forEach((button) => {
    button.addEventListener("click", () => {
      button.closest(".modal-overlay")?.classList.add("hidden");
    });
  });

  $("#settingsBtn")?.addEventListener("click", () => {
    $("#settingsModal")?.classList.remove("hidden");
  });

  $("#settingsForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const foregroundTimeout = Number($("#settingForegroundTimeout")?.value || 50);
    const maxPerUmo = Number($("#settingMaxPerUmo")?.value || 5);
    const maxGlobal = Number($("#settingMaxGlobal")?.value || 20);
    const retentionDays = Number($("#settingRetention")?.value || 30);
    const payload = {
      default_agent_name: $("#settingDefaultAgent")?.value.trim() || "",
      allowed_agent_names: ($("#settingAllowedAgents")?.value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      hide_native_tools: Boolean($("#settingHideNative")?.checked),
      hide_transfer_tools: Boolean($("#settingHideTransfer")?.checked),
      include_raw_user_input: Boolean($("#settingRawInput")?.checked),
      log_raw_llm_io: Boolean($("#settingLogRaw")?.checked),
      foreground_timeout_seconds: Math.min(55, Math.max(1, foregroundTimeout)),
      memory_agent_names: ($("#settingMemoryAgents")?.value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      max_active_per_umo: Math.max(1, maxPerUmo),
      max_active_global: Math.max(1, maxGlobal),
      retention_days: Math.max(1, retentionDays),
      dispatch_prompt_template: $("#settingPrompt")?.value || "",
    };
    try {
      const res = await apiPost("console/settings", payload);
      state.settings = res.config || payload;
      renderAgentOptions();
      $("#settingsModal")?.classList.add("hidden");
      toast("配置已保存");
    } catch (err) {
      toast(err.message || "保存失败");
    }
  });

  $("#toggleLeft")?.addEventListener("click", () => {
    const pane = $("#paneLeft");
    if (pane) pane.style.display = pane.style.display === "none" ? "flex" : "none";
  });

  window.addEventListener("beforeunload", () => {
    if (state.subscriptionId && bridge?.unsubscribeSSE) {
      bridge.unsubscribeSSE(state.subscriptionId);
    }
    window.clearInterval(state.pollTimer);
  });
}

async function boot() {
  bindEvents();
  bridge = window.AstrBotPluginPage || null;
  if (!bridge) {
    setStreamState("signal-error", "桥接缺失");
    toast("页面桥接未加载");
    return;
  }

  await bridge.ready();
  setStreamState("signal-poll", "轮询同步");
  startPolling();
  await refreshConsole({ silent: true, keepSession: false });
  await subscribeStream();
}

boot().catch((err) => {
  setStreamState("signal-error", "启动失败");
  toast(err.message || "启动失败");
});
