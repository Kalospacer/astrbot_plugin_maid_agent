let bridge = window.AstrBotPluginPage || null;

const ACTIVE_STATUSES = new Set(["queued", "running", "stopping"]);
const RESPONSE_EVENT_TYPES = new Set(["agent_output", "agent_result", "tool_direct_message"]);
const QUIET_EVENT_TYPES = new Set(["queued", "finished"]);

const state = {
  tasks: [],
  umos: [],
  selectedUmo: "",
  selectedSessionId: "",
  sessionTasks: [],
  sessionEvents: {},
  agents: [],
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
  pendingDeleteTimer: 0,
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

async function loadTasks() {
  const data = await apiGet("console/tasks", { limit: 500 });
  state.tasks = data.tasks || [];
  updateKnownUmos();
  renderUmoSwitcher();
  updateSessionList();
}

async function loadSession(taskId, { scrollMode = "bottom" } = {}) {
  if (!taskId) {
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

async function refreshConsole({ silent = false, keepSession = true } = {}) {
  if (state.refreshInFlight) {
    state.refreshQueued = true;
    return;
  }

  state.refreshInFlight = true;
  const selectedBefore = state.selectedSessionId;
  try {
    await loadTasks();
    await Promise.allSettled([loadOverview(), loadAgents()]);

    if (keepSession && selectedBefore) {
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

  const rootTasks = state.tasks
    .filter((task) => task.unified_msg_origin === state.selectedUmo && !task.parent_task_id)
    .sort((a, b) => {
      const pinnedDelta = Number(isPinned(b)) - Number(isPinned(a));
      if (pinnedDelta !== 0) return pinnedDelta;
      return new Date(b.updated_at) - new Date(a.updated_at);
    });

  if (rootTasks.length === 0) {
    list.innerHTML = `<div class="empty-state">暂无历史对话</div>`;
    return;
  }

  list.innerHTML =
    `<div class="session-group-title">最近对话</div>` +
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
    let thinkingHtml = "";
    let responseHtml = "";

    for (const event of events) {
      if (RESPONSE_EVENT_TYPES.has(event.event_type)) {
        if (event.message) responseHtml += `${escapeHtml(event.message)}\n\n`;
        continue;
      }
      if (QUIET_EVENT_TYPES.has(event.event_type) || event.event_type?.startsWith("system")) {
        continue;
      }
      thinkingHtml += `<div class="thinking-line"><strong>${escapeHtml(event.title || event.event_type)}</strong>${event.message ? ` - ${escapeHtml(event.message)}` : ""}</div>`;
    }

    const isRunning = ACTIVE_STATUSES.has(task.status);
    if (thinkingHtml || responseHtml || isRunning || task.status === "error") {
      html += `
        <div class="chat-message maid">
          <div class="chat-message-inner">
            <div class="chat-avatar">M</div>
            <div class="assistant-flow">
      `;

      if (thinkingHtml || isRunning) {
        html += `
          <details class="thinking-block" data-thinking-key="${escapeHtml(getThinkingKey(task))}" ${getThinkingOpenAttribute(task, isRunning)}>
            <summary class="thinking-summary">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transform: rotate(90deg);"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
              ${isRunning ? "运行中" : "过程"}
            </summary>
            <div class="thinking-details">${thinkingHtml || "等待后台更新"}</div>
          </details>
        `;
      }

      if (responseHtml) {
        html += `<div class="bubble">${responseHtml.trim()}</div>`;
      } else if (task.status === "error") {
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

function renderToolChain(task) {
  const node = $("#toolChain");
  if (!node) return;
  const chain = taskMeta(task).tool_chain || {};
  const entries = Array.isArray(chain.entries) ? chain.entries : [];
  const rawMessages = Array.isArray(chain.messages) ? chain.messages : [];

  if (entries.length === 0 && rawMessages.length === 0) {
    node.innerHTML = `<div class="empty-state">暂无工具调用链</div>`;
    return;
  }

  const entriesHtml = entries
    .map((entry) => {
      const kind = entry.kind || "message";
      const body = stringifyStructuredValue(entry.message);
      const toolCallId = entry.tool_call_id
        ? `<span class="tool-call-id">${escapeHtml(compactId(entry.tool_call_id))}</span>`
        : "";
      return `
        <article class="tool-chain-item ${escapeHtml(kind)}">
          <div class="tool-chain-head">
            <span class="tool-chain-kind">${escapeHtml(kind)}</span>
            <span class="tool-chain-index">#${escapeHtml(entry.index ?? "")}</span>
            ${toolCallId}
          </div>
          <div class="tool-chain-title">${escapeHtml(entry.title || kind)}</div>
          ${body ? `<pre class="tool-chain-body">${escapeHtml(body)}</pre>` : ""}
        </article>
      `;
    })
    .join("");

  const rawHtml =
    rawMessages.length > 0
      ? `
        <details class="tool-chain-raw">
          <summary>原始 messages (${escapeHtml(rawMessages.length)})</summary>
          <pre class="raw-box">${escapeHtml(JSON.stringify(rawMessages, null, 2))}</pre>
        </details>
      `
      : "";

  node.innerHTML = `${entriesHtml}${rawHtml}`;
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
    <dt>来源 UMO</dt><dd>${escapeHtml(task.unified_msg_origin)}</dd>
    <dt>更新时间</dt><dd>${escapeHtml(formatTime(task.updated_at))}</dd>
  `;

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

    const parentId = state.selectedSessionId || "";
    res = await apiPost("console/actions/dispatch", {
      unified_msg_origin: umo,
      agent_name: agent,
      request_text: text,
      parent_task_id: parentId,
    });
    if (!res.task) throw new Error(res.error || "派发失败");
    mergeTask(res.task);
    updateKnownUmos();
    state.selectedUmo = res.task.unified_msg_origin || state.selectedUmo;
    await loadSession(parentId || res.task.task_id, { scrollMode: "bottom" });
    toast(parentId ? "已发送到当前会话" : "已创建会话");
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

async function doneCurrentSession() {
  const task = state.detail?.task || getSelectedRootTask();
  const payload = task
    ? { task_id: task.task_id }
    : { unified_msg_origin: state.selectedUmo };
  try {
    await apiPost("console/actions/done", payload);
    toast("已结束 Session");
    await refreshConsole({ silent: true, keepSession: true });
  } catch (err) {
    toast(err.message || "结束失败");
  }
}

async function rerunCurrentTask() {
  const task = state.detail?.task;
  if (!task) return;
  try {
    const res = await apiPost("console/actions/rerun", { task_id: task.task_id });
    if (!res.task) throw new Error(res.error || "重跑失败");
    mergeTask(res.task);
    await loadSession(res.task.parent_task_id || res.task.task_id, { scrollMode: "bottom" });
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

  if (data.type === "task" && data.task) {
    mergeTask(data.task);
    updateKnownUmos();
    renderUmoSwitcher();
    updateSessionList();
    if (
      state.selectedSessionId &&
      (data.task.task_id === state.selectedSessionId ||
        data.task.parent_task_id === state.selectedSessionId)
    ) {
      await loadSession(state.selectedSessionId, { scrollMode: "preserve" });
    }
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
  $("#settingSession") && ($("#settingSession").checked = Boolean(config.session_enabled));
  $("#settingLogRaw") && ($("#settingLogRaw").checked = Boolean(config.log_raw_llm_io));
  $("#settingTimeout") &&
    ($("#settingTimeout").value = config.session_timeout_minutes ?? "");
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
    const actionButton = target.closest("[data-session-action]");
    if (!actionButton && target.closest(".session-rename-form")) return;

    if (actionButton) {
      event.stopPropagation();
      const action = actionButton.dataset.sessionAction;
      if (action === "delete") requestDeleteSession(taskId);
      if (action === "rename") startRenameSession(taskId);
      if (action === "pin") await togglePinSession(taskId);
      return;
    }

    await loadSession(taskId);
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
  $("#doneButton")?.addEventListener("click", doneCurrentSession);
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
    const timeoutValue = Number($("#settingTimeout")?.value || 0);
    const payload = {
      default_agent_name: $("#settingDefaultAgent")?.value.trim() || "",
      allowed_agent_names: ($("#settingAllowedAgents")?.value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      hide_native_tools: Boolean($("#settingHideNative")?.checked),
      hide_transfer_tools: Boolean($("#settingHideTransfer")?.checked),
      include_raw_user_input: Boolean($("#settingRawInput")?.checked),
      session_enabled: Boolean($("#settingSession")?.checked),
      log_raw_llm_io: Boolean($("#settingLogRaw")?.checked),
      session_timeout_minutes: timeoutValue > 0 ? timeoutValue : 60,
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
