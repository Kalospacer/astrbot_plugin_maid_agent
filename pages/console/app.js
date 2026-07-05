const bridge = window.AstrBotPluginPage;

const state = {
  tasks: [],
  detail: null,
  overview: null,
  agents: [],
  settings: null,
  selectedTaskId: "",
  subscriptionId: "",
  activeTab: "detail",
};

const $ = (selector) => document.querySelector(selector);

const statusText = {
  queued: "排队",
  running: "运行",
  stopping: "停止中",
  done: "完成",
  partial_done: "部分完成",
  error: "异常",
  stopped: "停止",
};

const stages = [
  ["queued", "call_maid"],
  ["dispatch", "running"],
  ["agent_output", "tool_direct_message"],
  ["agent_result", "follow_up_request"],
  ["follow_up_sent"],
  ["finished", "error", "stopped"],
];

const stageLabels = ["call_maid", "dispatch", "runner", "tool/progress", "follow-up", "sent"];

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

function normalizeTask(task) {
  return {
    task_id: "",
    title: "",
    status: "queued",
    kind: "single",
    source: "chat",
    agent_name: "",
    request_text: "",
    unified_msg_origin: "",
    sender_id: "",
    updated_at: "",
    created_at: "",
    completed_at: "",
    meta: {},
    ...task,
  };
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
}

function setStreamState(kind, text) {
  const node = $("#streamState");
  node.textContent = text;
  node.className = `signal ${kind}`;
}

function selectedTask() {
  if (!state.detail?.task) return null;
  return normalizeTask(state.detail.task);
}

async function loadOverview() {
  state.overview = await bridge.apiGet("console/overview");
  const statuses = state.overview.statuses || {};
  $("#countRunning").textContent = Number(statuses.running || 0) + Number(statuses.stopping || 0);
  $("#countDone").textContent = Number(statuses.done || 0) + Number(statuses.partial_done || 0);
  $("#countError").textContent = Number(statuses.error || 0) + Number(statuses.stopped || 0);
  if (state.overview.config) {
    fillSettings(state.overview.config);
  }
}

async function loadTasks(keepSelection = true) {
  const query = $("#searchInput").value.trim();
  const status = $("#statusFilter").value;
  const data = await bridge.apiGet("console/tasks", { limit: 160, query, status });
  state.tasks = (data.tasks || []).map(normalizeTask);
  renderTasks();
  if (!keepSelection || !state.selectedTaskId) return;
  if (state.tasks.some((task) => task.task_id === state.selectedTaskId)) {
    await loadDetail(state.selectedTaskId);
  }
}

async function loadDetail(taskId) {
  if (!taskId) return;
  state.selectedTaskId = taskId;
  state.detail = await bridge.apiGet(`console/tasks/${encodeURIComponent(taskId)}`);
  renderAll();
}

async function loadAgents() {
  const data = await bridge.apiGet("console/subagents");
  state.agents = data.agents || [];
  renderAgentOptions();
}

async function loadSettings() {
  const data = await bridge.apiGet("console/settings");
  state.settings = data.config || {};
  fillSettings(state.settings);
}

function renderAgentOptions() {
  const select = $("#dispatchAgent");
  const configuredDefault = state.settings?.default_agent_name || state.overview?.config?.default_agent_name || "butler";
  const names = [...new Set([configuredDefault, ...state.agents.map((agent) => agent.name)].filter(Boolean))];
  select.innerHTML = names
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join("");
}

function renderTasks() {
  const list = $("#taskList");
  if (!state.tasks.length) {
    list.innerHTML = '<div class="empty-state"><p>暂无任务</p></div>';
    return;
  }
  list.innerHTML = state.tasks
    .map((rawTask) => {
      const task = normalizeTask(rawTask);
      const active = task.task_id === state.selectedTaskId ? " active" : "";
      const status = statusText[task.status] || task.status;
      return `
        <button class="task-row${active}" type="button" data-task-id="${escapeHtml(task.task_id)}">
          <span class="status-pill status-${escapeHtml(task.status)}">${escapeHtml(status)}</span>
          <strong>${escapeHtml(task.title || task.request_text || task.task_id)}</strong>
          <small>${escapeHtml(task.agent_name || "agent")} · ${escapeHtml(task.kind)} · ${escapeHtml(task.source)}</small>
          <span class="task-meta">
            <span>${escapeHtml(compactId(task.task_id))}</span>
            <span>${escapeHtml(formatTime(task.updated_at || task.created_at))}</span>
          </span>
        </button>
      `;
    })
    .join("");
}

function eventStageIndex(eventType) {
  const normalized = String(eventType || "");
  const index = stages.findIndex((items) => items.includes(normalized));
  return index >= 0 ? index : 2;
}

function renderStageRail() {
  const events = state.detail?.events || [];
  const task = selectedTask();
  const maxEventStage = events.reduce((max, event) => Math.max(max, eventStageIndex(event.event_type)), 0);
  const terminalError = task && ["error", "stopped"].includes(task.status);
  $("#stageRail").innerHTML = stageLabels
    .map((label, index) => {
      const active = index === maxEventStage && task && !["done", "partial_done", "error", "stopped"].includes(task.status);
      const done = index < maxEventStage || (task && ["done", "partial_done"].includes(task.status));
      const error = terminalError && index === stageLabels.length - 1;
      const className = ["stage", active ? "active" : "", done ? "done" : "", error ? "error" : ""]
        .filter(Boolean)
        .join(" ");
      return `
        <div class="${className}">
          <span class="stage-dot"></span>
          <span class="stage-label">${escapeHtml(label)}</span>
        </div>
      `;
    })
    .join("");
}

function renderEvents() {
  const task = selectedTask();
  $("#selectedTitle").textContent = task ? task.title || task.request_text || task.task_id : "选择一个任务";
  const list = $("#eventList");
  const events = state.detail?.events || [];
  if (!events.length) {
    list.className = "event-list empty-state";
    list.innerHTML = "<p>暂无事件</p>";
    return;
  }
  list.className = "event-list";
  list.innerHTML = events
    .map((event) => {
      const type = event.event_type || "event";
      const message = event.message || "";
      return `
        <article class="event-item">
          <time class="event-time">${escapeHtml(formatTime(event.created_at))}</time>
          <div class="event-body">
            <header>
              <strong>${escapeHtml(event.title || type)}</strong>
              <span class="status-pill status-${escapeHtml(event.status || type)}">${escapeHtml(event.source || "system")}</span>
            </header>
            <div class="event-meta">${escapeHtml(type)}</div>
            ${message ? `<div class="event-message">${escapeHtml(message)}</div>` : ""}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderInspector() {
  const task = selectedTask();
  $("#inspectorTitle").textContent = task ? compactId(task.task_id) : "未选择";
  const disabled = !task;
  $("#stopButton").disabled = disabled || !["queued", "running", "stopping"].includes(task?.status);
  $("#rerunButton").disabled = disabled;
  $("#doneButton").disabled = disabled;
  $("#steerText").disabled = disabled || task?.kind === "batch";

  if (!task) {
    $("#taskFacts").innerHTML = "";
    $("#rawRequest").textContent = "";
    $("#rawMeta").textContent = "";
    $("#actionList").innerHTML = '<div class="empty-state"><p>暂无操作</p></div>';
    return;
  }

  const facts = [
    ["状态", statusText[task.status] || task.status],
    ["类型", task.kind],
    ["来源", task.source],
    ["Agent", task.agent_name || "-"],
    ["UMO", task.unified_msg_origin || "-"],
    ["Sender", task.sender_id || "-"],
    ["创建", formatTime(task.created_at)],
    ["更新", formatTime(task.updated_at)],
  ];
  $("#taskFacts").innerHTML = facts
    .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");
  $("#rawRequest").textContent = task.request_text || "";
  $("#rawMeta").textContent = JSON.stringify(task.meta || {}, null, 2);

  const actions = state.detail?.actions || [];
  $("#actionList").innerHTML = actions.length
    ? actions
        .map(
          (action) => `
            <div class="action-item">
              <strong>${escapeHtml(action.action)}</strong>
              <small>${escapeHtml(action.source)} · ${escapeHtml(formatTime(action.created_at))}</small>
              ${action.result_text ? `<div class="event-message">${escapeHtml(action.result_text)}</div>` : ""}
            </div>
          `,
        )
        .join("")
    : '<div class="empty-state"><p>暂无操作</p></div>';
}

function renderAll() {
  renderTasks();
  renderStageRail();
  renderEvents();
  renderInspector();
}

function setTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${tab}Tab`);
  });
}

function fillSettings(config) {
  if (!config) return;
  state.settings = config;
  $("#settingDefaultAgent").value = config.default_agent_name || "";
  $("#settingAllowedAgents").value = (config.allowed_agent_names || []).join(", ");
  $("#settingHideNative").checked = Boolean(config.hide_native_tools);
  $("#settingHideTransfer").checked = Boolean(config.hide_transfer_tools);
  $("#settingRawInput").checked = Boolean(config.include_raw_user_input);
  $("#settingSession").checked = Boolean(config.session_enabled);
  $("#settingLogRaw").checked = Boolean(config.log_raw_llm_io);
  $("#settingTimeout").value = config.session_timeout_minutes || 20;
  $("#settingPrompt").value = config.dispatch_prompt_template || "";
  renderAgentOptions();
}

function readSettingsForm() {
  return {
    default_agent_name: $("#settingDefaultAgent").value.trim(),
    allowed_agent_names: $("#settingAllowedAgents")
      .value.split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    hide_native_tools: $("#settingHideNative").checked,
    hide_transfer_tools: $("#settingHideTransfer").checked,
    include_raw_user_input: $("#settingRawInput").checked,
    session_enabled: $("#settingSession").checked,
    log_raw_llm_io: $("#settingLogRaw").checked,
    session_timeout_minutes: Number($("#settingTimeout").value || 20),
    dispatch_prompt_template: $("#settingPrompt").value,
  };
}

function upsertTask(task) {
  const normalized = normalizeTask(task);
  const index = state.tasks.findIndex((item) => item.task_id === normalized.task_id);
  if (index >= 0) {
    state.tasks[index] = normalized;
  } else {
    state.tasks.unshift(normalized);
  }
  state.tasks.sort((a, b) => String(b.updated_at || b.created_at).localeCompare(String(a.updated_at || a.created_at)));
}

async function handleSseMessage(message) {
  const item = message?.parsed || message;
  if (!item || typeof item !== "object") return;
  if (item.type === "reset") {
    state.selectedTaskId = "";
    state.detail = null;
    await loadOverview();
    await loadTasks(false);
    renderAll();
    return;
  }
  if (item.type === "task" && item.task) {
    upsertTask(item.task);
    if (item.task.task_id === state.selectedTaskId) {
      state.detail = { ...(state.detail || {}), task: normalizeTask(item.task) };
    }
    renderAll();
    await loadOverview();
    return;
  }
  if (item.type === "event" && item.event) {
    if (item.event.task_id === state.selectedTaskId && state.detail) {
      state.detail.events = [...(state.detail.events || []), item.event];
      renderAll();
    }
    return;
  }
  if (item.type === "action" && item.action) {
    if (item.action.task_id === state.selectedTaskId && state.detail) {
      state.detail.actions = [...(state.detail.actions || []), item.action];
      renderInspector();
    }
  }
}

async function subscribeStream() {
  if (state.subscriptionId) {
    await bridge.unsubscribeSSE(state.subscriptionId);
  }
  state.subscriptionId = await bridge.subscribeSSE("console/stream", {
    onOpen() {
      setStreamState("signal-live", "在线");
    },
    onMessage(event) {
      void handleSseMessage(event);
    },
    onError() {
      setStreamState("signal-error", "断开");
    },
  });
}

async function refreshAll() {
  await loadOverview();
  await loadAgents();
  await loadSettings();
  await loadTasks();
  renderAll();
}

function bindEvents() {
  $("#taskList").addEventListener("click", (event) => {
    const row = event.target.closest("[data-task-id]");
    if (!row) return;
    void loadDetail(row.dataset.taskId);
  });

  $("#filterForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void loadTasks(false);
  });
  $("#searchInput").addEventListener("input", () => {
    window.clearTimeout(bindEvents.searchTimer);
    bindEvents.searchTimer = window.setTimeout(() => void loadTasks(false), 220);
  });
  $("#statusFilter").addEventListener("change", () => void loadTasks(false));
  $("#refreshButton").addEventListener("click", () => void refreshAll());

  $("#dispatchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      unified_msg_origin: $("#dispatchUmo").value.trim(),
      agent_name: $("#dispatchAgent").value,
      request_text: $("#dispatchText").value.trim(),
      maid_full_reply: $("#dispatchReply").value.trim(),
    };
    try {
      const data = await bridge.apiPost("console/actions/dispatch", payload);
      $("#dispatchText").value = "";
      $("#dispatchReply").value = "";
      if (data.task?.task_id) await loadDetail(data.task.task_id);
      toast("已派发");
    } catch (error) {
      toast(error.message || "派发失败");
    }
  });

  $("#steerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const task = selectedTask();
    if (!task) return;
    const messageText = $("#steerText").value.trim();
    if (!messageText) return;
    try {
      await bridge.apiPost("console/actions/steer", {
        task_id: task.task_id,
        message_text: messageText,
      });
      $("#steerText").value = "";
      toast("已发送补充");
    } catch (error) {
      toast(error.message || "补充失败");
    }
  });

  $("#stopButton").addEventListener("click", async () => {
    const task = selectedTask();
    if (!task) return;
    try {
      await bridge.apiPost("console/actions/stop", { task_id: task.task_id });
      toast("已请求停止");
    } catch (error) {
      toast(error.message || "停止失败");
    }
  });

  $("#rerunButton").addEventListener("click", async () => {
    const task = selectedTask();
    if (!task) return;
    try {
      const data = await bridge.apiPost("console/actions/rerun", { task_id: task.task_id });
      if (data.task?.task_id) await loadDetail(data.task.task_id);
      toast("已重跑");
    } catch (error) {
      toast(error.message || "重跑失败");
    }
  });

  $("#doneButton").addEventListener("click", async () => {
    const task = selectedTask();
    if (!task) return;
    try {
      await bridge.apiPost("console/actions/done", { task_id: task.task_id });
      toast("Session 已结束");
    } catch (error) {
      toast(error.message || "结束失败");
    }
  });

  $("#exportButton").addEventListener("click", () => {
    void bridge.download("console/export", {}, "maid-console-history.json");
  });

  $("#clearButton").addEventListener("click", async () => {
    try {
      await bridge.apiPost("console/clear", {});
      state.selectedTaskId = "";
      state.detail = null;
      await refreshAll();
      toast("历史已清空");
    } catch (error) {
      toast(error.message || "清空失败");
    }
  });

  $("#settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = await bridge.apiPost("console/settings", readSettingsForm());
      fillSettings(data.config);
      await loadAgents();
      toast("配置已保存");
    } catch (error) {
      toast(error.message || "保存失败");
    }
  });

  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  });
}

async function boot() {
  bindEvents();
  renderStageRail();
  await bridge.ready();
  await refreshAll();
  await subscribeStream();
  window.addEventListener("beforeunload", () => {
    if (state.subscriptionId) {
      void bridge.unsubscribeSSE(state.subscriptionId);
    }
  });
}

boot().catch((error) => {
  setStreamState("signal-error", "错误");
  toast(error.message || "控制台启动失败");
});
