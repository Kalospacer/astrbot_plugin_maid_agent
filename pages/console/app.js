let bridge = window.AstrBotPluginPage || null;

const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "stopping"]);

const POLL_INTERVAL_SSE_MS = 20000;
const POLL_INTERVAL_FALLBACK_MS = 5000;
const RUNTIME_REFRESH_EVERY = 3;

const state = {
  umos: [],
  selectedUmo: "",
  agents: [],
  agentNames: [],
  dispatchAgent: "",
  runtimeAgents: [],
  runtimeRuns: {},
  selectedAgentId: "",
  selectedViewRunId: "",
  agentTranscript: null,
  transcriptRunTrace: {},
  settings: null,
  overview: null,
  subscriptionId: "",
  streamLive: false,
  pollTimer: 0,
  pollCount: 0,
  durationTimer: 0,
  refreshInFlight: false,
  refreshQueued: false,
  thinkingOpenState: {},
  lastFeedFingerprint: "",
  lastSessionListFingerprint: "",
  pendingDeleteAgentId: "",
  pendingDeleteAgentTimer: 0,
  attachments: [],
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

/* 轻量 Markdown → HTML（无 vendor）。只输出转义后的安全标签，覆盖 ChatUI 常见结构。 */
function renderInlineMarkdown(text) {
  let s = escapeHtml(String(text ?? ""));
  // images ![alt](url)
  s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g, (_m, alt, url) => {
    return `<img src="${url}" alt="${alt}" loading="lazy">`;
  });
  // links [text](url)
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_m, label, url) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  // bold / italic / strike / code（顺序：code 先，再粗体，再斜体）
  s = s.replace(/`([^`\n]+)`/g, (_m, code) => `<code>${code}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
  s = s.replace(/(?<!_)_([^_\n]+)_(?!_)/g, "<em>$1</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  // soft line breaks inside paragraphs
  s = s.replace(/\n/g, "<br>");
  return s;
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|-]*\|?\s*$/.test(line) && /\|/.test(line) && /-+/.test(line);
}

function splitTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function renderMarkdown(text) {
  const raw = String(text ?? "").replace(/\r\n?/g, "\n").trim();
  if (!raw) return "";

  const lines = raw.split("\n");
  const html = [];
  let i = 0;
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInlineMarkdown(paragraph.join("\n"))}</p>`);
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    const fence = line.match(/^```([\w-]*)\s*$/);
    if (fence) {
      flushParagraph();
      const lang = fence[1] || "";
      const body = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1; // closing ```
      const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
      html.push(`<pre><code${cls}>${escapeHtml(body.join("\n"))}</code></pre>`);
      continue;
    }

    // horizontal rule
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph();
      html.push("<hr>");
      i += 1;
      continue;
    }

    // heading
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    // blockquote
    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
      continue;
    }

    // table (GFM-ish)
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      flushParagraph();
      const header = splitTableRow(line);
      i += 2; // skip separator
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      const thead = `<thead><tr>${header.map((c) => `<th>${renderInlineMarkdown(c)}</th>`).join("")}</tr></thead>`;
      const tbody = rows.length
        ? `<tbody>${rows
            .map(
              (row) =>
                `<tr>${header
                  .map((_, idx) => `<td>${renderInlineMarkdown(row[idx] || "")}</td>`)
                  .join("")}</tr>`,
            )
            .join("")}</tbody>`
        : "";
      html.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    // unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }

    // ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }

    // blank line ends paragraph
    if (!line.trim()) {
      flushParagraph();
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
  }
  flushParagraph();
  return html.join("");
}

function afterMarkdownSanitize(root) {
  if (!root) return;
  root.querySelectorAll?.("a[href]")?.forEach((anchor) => {
    const href = anchor.getAttribute("href") || "";
    if (!/^https?:\/\//i.test(href)) {
      anchor.removeAttribute("href");
      return;
    }
    anchor.setAttribute("target", "_blank");
    anchor.setAttribute("rel", "noopener noreferrer");
  });
  root.querySelectorAll?.("img[src]")?.forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (!/^https?:\/\//i.test(src)) img.remove();
  });
}

function compactId(value) {
  const text = String(value || "");
  return text.length > 12 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

// 50 个二次元女角色名（真实动画作品），按 agent_id 稳定映射，避免在 UI 上暴露哈希。
const ANIME_ALIASES = [
  "Ganyu", "Mafuyu", "Sakiko", "Hu Tao", "Raiden Shogun", "Yae Miko", "Nahida",
  "Furina", "Navia", "Arlecchino", "Shenhe", "Yelan", "Nilou", "Kaveh",
  "Alhaitham", "Eula", "Ayaka", "Yoimiya", "Itto", "Sara",
  "Miko", "Collei", "Lumine", "Aether", "Paimon",
  "Makima", "Power", "Kobeni", "Himeno", "Reze",
  "Yor", "Anya", "Becky", "Fiona", "Sylvia",
  "Rika", "Touko", "Sayaka", "Kyouko", "Homura",
  "Madoka", "Mami", "Nagisa", "Hitomi", "Kyoko",
  "Reina", "Kumiko", "Asuka", "Haruka", "Aoi",
];

function aliasForAgentId(agentId) {
  const id = String(agentId || "");
  if (!id) return "Unknown";
  // FNV-1a 哈希 → 稳定落到别名表，同一 agent_id 永远同名。
  let hash = 2166136261;
  for (let i = 0; i < id.length; i += 1) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const idx = Math.abs(hash) % ANIME_ALIASES.length;
  return ANIME_ALIASES[idx];
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

function updateLiveDurations() {
  const now = new Date().toISOString();
  $$('[data-live-duration-start]').forEach((node) => {
    const duration = formatDuration(node.dataset.liveDurationStart, now);
    if (duration) node.textContent = duration;
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
  node.className = `signal ${kind} sidebar-label`;
  const wasLive = state.streamLive;
  state.streamLive = kind === "signal-active";
  if (wasLive !== state.streamLive) startPolling();
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

async function uploadFile(endpoint, file) {
  const pageBridge = ensureBridge();
  if (typeof pageBridge.upload !== "function") {
    throw new Error("当前 AstrBot 页面桥不支持文件上传，请刷新 Dashboard。");
  }
  return pageBridge.upload(endpoint, file);
}

function formatFileSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachments() {
  const strip = $("#attachmentStrip");
  if (!strip) return;
  strip.classList.toggle("hidden", state.attachments.length === 0);
  strip.innerHTML = state.attachments
    .map((attachment) => {
      const stateLabel =
        attachment.status === "uploading"
          ? "上传中"
          : attachment.status === "error"
            ? "上传失败"
            : formatFileSize(attachment.size);
      return `
        <article class="attachment-chip ${escapeHtml(attachment.status)}" title="${escapeHtml(attachment.error || attachment.name)}">
          <img src="${escapeHtml(attachment.previewUrl)}" alt="">
          <span class="attachment-meta">
            <strong>${escapeHtml(attachment.name)}</strong>
            <small>${escapeHtml(stateLabel)}</small>
          </span>
          <button type="button" class="attachment-remove" data-remove-attachment="${escapeHtml(attachment.id)}" aria-label="移除 ${escapeHtml(attachment.name)}">×</button>
        </article>
      `;
    })
    .join("");
}

async function uploadAttachment(attachment) {
  try {
    const result = await uploadFile("console/upload", attachment.file);
    if (!result?.path) throw new Error("上传接口没有返回图片路径。");
    attachment.path = result.path;
    attachment.size = result.size ?? attachment.size;
    attachment.status = "ready";
  } catch (err) {
    attachment.status = "error";
    attachment.error = err.message || "图片上传失败";
  } finally {
    renderAttachments();
  }
  return attachment;
}

function addImageFiles(fileList) {
  const allowedTypes = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
  const files = Array.from(fileList || []);
  let skipped = 0;
  for (const file of files) {
    if (state.attachments.length >= 5) {
      skipped += 1;
      continue;
    }
    if (!allowedTypes.has(file.type) || file.size <= 0 || file.size > 10 * 1024 * 1024) {
      skipped += 1;
      continue;
    }
    const attachment = {
      id: `attachment_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      file,
      name: file.name || "image",
      size: file.size,
      previewUrl: URL.createObjectURL(file),
      path: "",
      status: "uploading",
      error: "",
      uploadPromise: null,
    };
    state.attachments.push(attachment);
    attachment.uploadPromise = uploadAttachment(attachment);
  }
  renderAttachments();
  if (skipped) toast("部分图片未添加：仅支持 JPEG/PNG/WEBP/GIF，最多 5 张且每张不超过 10 MB");
}

function removeAttachment(attachmentId) {
  const index = state.attachments.findIndex((item) => item.id === attachmentId);
  if (index < 0) return;
  const [attachment] = state.attachments.splice(index, 1);
  if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  renderAttachments();
}

function clearAttachments() {
  for (const attachment of state.attachments) {
    if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  }
  state.attachments = [];
  renderAttachments();
  const input = $("#imageInput");
  if (input) input.value = "";
}

async function readyAttachmentPaths() {
  await Promise.all(
    state.attachments.map((attachment) => attachment.uploadPromise).filter(Boolean),
  );
  const failed = state.attachments.find((attachment) => attachment.status === "error");
  if (failed) throw new Error(`${failed.name}：${failed.error || "上传失败"}`);
  return state.attachments
    .filter((attachment) => attachment.status === "ready" && attachment.path)
    .map((attachment) => attachment.path);
}

function clearPromptComposer(input) {
  input.value = "";
  input.style.height = "auto";
  clearAttachments();
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

function getSelectedAgent() {
  return state.runtimeAgents.find((agent) => agent.agent_id === state.selectedAgentId) || null;
}

function displayAgentTitle(agent) {
  if (!agent) return "";
  return agent.title || `${aliasForAgentId(agent.agent_id)} · ${agent.agent_name}`;
}

function getSelectedRuns() {
  if (!state.selectedAgentId) return [];
  return [...(state.runtimeRuns[state.selectedAgentId] || [])].sort(
    (a, b) => new Date(a.created_at) - new Date(b.created_at),
  );
}

function getViewRun() {
  const runs = getSelectedRuns();
  if (!runs.length) return null;
  return runs.find((run) => run.task_id === state.selectedViewRunId) || runs[runs.length - 1];
}

function updateKnownUmos() {
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

async function loadOverview({ applyForm = true } = {}) {
  state.overview = await apiGet("console/overview");
  state.settings = state.overview.config || state.settings;
  if (applyForm && state.settings) fillSettings(state.settings);
  if (!state.dispatchAgent && state.settings?.default_agent_name) {
    state.dispatchAgent = state.settings.default_agent_name;
    syncDispatchAgentLabel();
  }
}

async function loadAgents() {
  const data = await apiGet("console/subagents");
  state.agents = data.agents || [];
  renderAgentOptions();
}

async function loadRuntimeAgents({ force = false } = {}) {
  if (!force && state.pollCount > 0 && state.pollCount % RUNTIME_REFRESH_EVERY !== 0) {
    return;
  }
  const data = await apiGet("console/agents");
  state.runtimeAgents = data.agents || [];
  updateKnownUmos();
  renderUmoSwitcher();
  updateSessionList();
}

async function loadAgentTranscript(agentId, { scrollMode = "preserve" } = {}) {
  if (!agentId) return;
  const scrollSnapshot = captureFeedScroll();
  const [transcriptData, runsData] = await Promise.all([
    apiGet(`console/agents/${encodeURIComponent(agentId)}/transcript`),
    apiGet(`console/agents/${encodeURIComponent(agentId)}/runs`),
  ]);
  state.agentTranscript = transcriptData;
  state.runtimeRuns[agentId] = runsData.runs || [];
  state.transcriptRunTrace = {};
  renderChatFeed();
  renderInspector();
  updateSessionList();
  applyFeedScroll(scrollSnapshot, scrollMode);
}

async function loadAgent(agentId, { scrollMode = "bottom" } = {}) {
  // 选中态唯一权威来源：先写选中，再异步拉数据；拉取失败不改选中。
  state.selectedAgentId = agentId || "";
  state.selectedViewRunId = "";
  state.transcriptRunTrace = {};
  if (!agentId) {
    state.agentTranscript = null;
    state.lastFeedFingerprint = "";
    renderChatFeed();
    renderInspector();
    updateSessionList();
    return;
  }
  try {
    await loadAgentTranscript(agentId, { scrollMode });
  } catch (err) {
    toast(err.message || "加载对话失败");
    updateSessionList();
  }
}

async function refreshConsole({ silent = false, keepSession = true } = {}) {
  if (state.refreshInFlight) {
    state.refreshQueued = true;
    return;
  }

  state.refreshInFlight = true;
  try {
    state.pollCount += 1;
    await loadRuntimeAgents({ force: !silent });
    await Promise.allSettled([
      loadOverview({ applyForm: !silent }),
      loadAgents(),
    ]);

    if (keepSession && state.selectedAgentId) {
      const exists = state.runtimeAgents.some(
        (agent) => agent.agent_id === state.selectedAgentId,
      );
      if (exists) {
        await loadAgentTranscript(state.selectedAgentId, { scrollMode: "preserve" });
      } else {
        await loadAgent("");
      }
    } else if (!state.selectedAgentId) {
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
  const interval = state.streamLive ? POLL_INTERVAL_SSE_MS : POLL_INTERVAL_FALLBACK_MS;
  state.pollTimer = window.setInterval(() => {
    refreshConsole({ silent: true, keepSession: true });
  }, interval);
}

function startDurationTicker() {
  window.clearInterval(state.durationTimer);
  updateLiveDurations();
  state.durationTimer = window.setInterval(updateLiveDurations, 1000);
}

function getConfiguredDefaultAgent() {
  return state.settings?.default_agent_name || state.overview?.config?.default_agent_name || "butler";
}

function getAgentNames() {
  const configuredDefault = getConfiguredDefaultAgent();
  return [...new Set([configuredDefault, ...state.agents.map((agent) => agent.name)].filter(Boolean))];
}

function syncDispatchAgentLabel() {
  const label = $("#dispatchAgentLabel");
  if (label) label.textContent = state.dispatchAgent || getConfiguredDefaultAgent();
}

function renderAgentOptions() {
  const names = getAgentNames();
  const namesKey = names.join("\0");
  const prevKey = state.agentNames.join("\0");
  state.agentNames = names;

  if (!state.dispatchAgent || !names.includes(state.dispatchAgent)) {
    state.dispatchAgent = names[0] || getConfiguredDefaultAgent();
  }
  syncDispatchAgentLabel();

  // names unchanged and selection still valid — menu will rebuild on open
  if (namesKey === prevKey) return;
}

function closeAgentMenu() {
  const menu = $("#agentMenu");
  const btn = $("#dispatchAgentBtn");
  menu?.classList.add("hidden");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function openAgentMenu() {
  const menu = $("#agentMenu");
  const btn = $("#dispatchAgentBtn");
  if (!menu || !btn) return;
  closeSessionMenu();
  const names = getAgentNames();
  if (!names.length) {
    menu.innerHTML = `<div class="menu-item" disabled>暂无 Agent</div>`;
  } else {
    menu.innerHTML = names
      .map((name) => {
        const active = name === state.dispatchAgent;
        return `
          <button class="menu-item ${active ? "active" : ""}" type="button" role="menuitem"
            data-agent-name="${escapeHtml(name)}">
            <span class="check" aria-hidden="true">${active ? "✓" : ""}</span>
            <span>${escapeHtml(name)}</span>
          </button>
        `;
      })
      .join("");
  }
  menu.classList.remove("hidden");
  btn.setAttribute("aria-expanded", "true");
  const rect = btn.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const top = Math.min(rect.top - menuRect.height - 6, window.innerHeight - menuRect.height - 8);
  menu.style.top = `${Math.max(8, top)}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.right - menuRect.width, window.innerWidth - menuRect.width - 8))}px`;
}

function toggleAgentMenu() {
  const menu = $("#agentMenu");
  if (!menu) return;
  if (menu.classList.contains("hidden")) openAgentMenu();
  else closeAgentMenu();
}

function buildFeedFingerprint() {
  const agent = getSelectedAgent();
  const segments = state.agentTranscript?.runs || [];
  const runs = getSelectedRuns();
  const taskPart = segments
    .map((segment) => {
      const chain = segment.tool_chain || {};
      const entries = Array.isArray(chain.entries) ? chain.entries : [];
      const last = entries[entries.length - 1];
      const run = runs.find((item) => item.task_id === segment.task_id);
      return [
        segment.task_id,
        run?.status || "",
        run?.updated_at || "",
        entries.length,
        last?.index ?? "",
        (run?.result || run?.error || "").length,
        segment.steers.length,
      ].join("~");
    })
    .join("|");
  const thinkingPart = Object.entries(state.thinkingOpenState)
    .map(([k, v]) => `${k}:${v ? 1 : 0}`)
    .join(",");
  return `${state.selectedAgentId}#${agent?.active_task_id || ""}#${taskPart}#${thinkingPart}`;
}

function buildSessionListFingerprint() {
  const runtimePart = state.runtimeAgents
    .filter((agent) => agent.unified_msg_origin === state.selectedUmo)
    .map((agent) =>
      [
        agent.agent_id,
        agent.agent_name,
        agent.title,
        agent.last_status,
        agent.updated_at,
        agent.last_run_at,
        agent.run_count,
        agent.pending_notification ? 1 : 0,
        agent.active_task_id,
      ].join(":"),
    )
    .join("|");
  return [
    state.selectedUmo,
    state.selectedAgentId,
    state.pendingDeleteAgentId,
    runtimePart,
  ].join("#");
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

  const fingerprint = buildSessionListFingerprint();
  if (fingerprint === state.lastSessionListFingerprint && list.childElementCount > 0) {
    return;
  }
  state.lastSessionListFingerprint = fingerprint;

  const runtimeAgents = state.runtimeAgents
    .filter((agent) => agent.unified_msg_origin === state.selectedUmo)
    .sort(
      (a, b) =>
        new Date(b.last_run_at || b.updated_at) - new Date(a.last_run_at || a.updated_at),
    );

  if (runtimeAgents.length === 0) {
    list.innerHTML = `<div class="empty-state">暂无 Agent 会话</div>`;
    return;
  }

  const MORE_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>`;

  list.innerHTML = runtimeAgents
    .map((agent) => {
      const deleting = agent.agent_id === state.pendingDeleteAgentId;
      const runCountLabel = agent.run_count > 1 ? `${agent.run_count} runs` : `${agent.run_count || 0} run`;
      const pendingLabel = agent.pending_notification ? " · 待通知" : "";
      const subtitle = [
        agent.agent_name,
        agent.last_status || "unknown",
        runCountLabel,
        formatTime(agent.last_run_at || agent.updated_at),
      ]
        .filter(Boolean)
        .join(" · ");
      return `
        <div class="session-item ${agent.agent_id === state.selectedAgentId ? "active" : ""} ${deleting ? "delete-armed" : ""}"
             data-id="${escapeHtml(agent.agent_id)}">
          <div class="session-copy">
            <div class="session-title">${escapeHtml(displayAgentTitle(agent))}</div>
            <small><span class="status-dot status-${escapeHtml(agent.last_status || "")}"></span>${escapeHtml(subtitle)}${escapeHtml(pendingLabel)}</small>
          </div>
          <div class="session-actions">
            <button class="session-action-btn" type="button" data-menu-kind="runtime" aria-label="更多操作" title="更多操作">${MORE_SVG}</button>
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

function renderChatHeader() {
  const node = $("#chatTitle");
  if (!node) return;
  const agent = getSelectedAgent();
  node.textContent = displayAgentTitle(agent);
}

function renderChatFeed() {
  const feed = $("#chatFeed");
  if (!feed) return;
  $(".pane-center")?.classList.toggle("centered", !state.selectedAgentId);
  renderChatHeader();

  if (!state.selectedAgentId) {
    feed.classList.add("is-empty");
    feed.innerHTML = "";
    state.lastFeedFingerprint = "";
    $("#promptText")?.focus();
    return;
  }

  captureThinkingOpenState();
  const fingerprint = buildFeedFingerprint();
  if (fingerprint === state.lastFeedFingerprint && feed.childElementCount > 0) {
    return;
  }
  state.lastFeedFingerprint = fingerprint;

  feed.classList.remove("is-empty");
  let html = "";

  const agent = getSelectedAgent();
  const runs = getSelectedRuns();
  const segments = state.agentTranscript?.runs || [];
  const mistressName = (state.agentTranscript?.mistress_name || "").trim();

  segments.forEach((segment, segmentIndex) => {
    const run = runs.find((item) => item.task_id === segment.task_id) || null;
    const status = run?.status || "completed";
    const isActive = ACTIVE_STATUSES.has(status);
    const pseudoTask = {
      task_id: segment.task_id,
      status,
      meta: { tool_chain: segment.tool_chain },
      created_at: run?.created_at || "",
      started_at: run?.started_at || run?.created_at || "",
      ended_at: run?.ended_at || run?.completed_at || run?.updated_at || "",
      updated_at: run?.updated_at || "",
    };

    if (segment.user_text) {
      const shown = displayUserText(segment.user_text);
      html += `
        <div class="chat-message user">
          <div class="chat-message-inner">
            <div class="bubble" title="${escapeHtml(segment.user_text)}">${escapeHtml(shown)}</div>
          </div>
        </div>
      `;
    }

    if (segment.mistress_text) {
      const shown = displayUserText(segment.mistress_text);
      const label = mistressName
        ? `<div class="mistress-label">${escapeHtml(mistressName)}</div>`
        : "";
      html += `
        <div class="chat-message mistress">
          <div class="chat-message-inner">
            <div class="chat-avatar" aria-hidden="true">✦</div>
            <div class="assistant-flow">
              ${label}
              <div class="bubble" title="${escapeHtml(segment.mistress_text)}">${escapeHtml(shown)}</div>
            </div>
          </div>
        </div>
      `;
    }

    const traceHtml = renderTracePanel(pseudoTask, "feed", "");
    const resultText = String(run?.result || "");
    const errorText = String(run?.error || "");
    const failed = ["error", "failed", "stopped", "interrupted"].includes(status);

    if (traceHtml || resultText || failed) {
      html += `
        <div class="chat-message maid">
          <div class="chat-message-inner">
            <div class="chat-avatar" aria-hidden="true">✦</div>
            <div class="assistant-flow">
      `;
      html += traceHtml;
      if (resultText) {
        html += `<div class="bubble markdown-body">${renderMarkdown(resultText)}</div>`;
      } else if (failed) {
        html += `<div class="bubble error-text">${escapeHtml(errorText || "任务发生异常，已终止。")}</div>`;
      }
      html += `
            </div>
          </div>
        </div>
      `;
    }

    for (const steerText of segment.steers) {
      html += `
        <div class="chat-message user">
          <div class="chat-message-inner">
            <div class="bubble" title="${escapeHtml(steerText)}">${escapeHtml(displayUserText(steerText))}</div>
          </div>
        </div>
      `;
    }

    if (segmentIndex < segments.length - 1) {
      html += `<div class="run-divider" aria-hidden="true"></div>`;
    }
  });

  if (!html) {
    const hint = agent?.active_task_id
      ? "该 Agent 正在运行，暂无对话记录"
      : "该 Agent 暂无对话记录";
    html = `<div class="empty-state">${hint}</div>`;
  }

  feed.innerHTML = html;
  afterMarkdownSanitize(feed);
}

function displayUserText(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return "";
  const blockMatch = raw.match(/【(?:大小姐请求|对方原话)】\s*\n?/);
  if (blockMatch) {
    const after = raw.slice(blockMatch.index + blockMatch[0].length).trim();
    const firstBlock = after.split(/【[^】]+】/)[0].trim();
    if (firstBlock) return firstBlock.length > 120 ? `${firstBlock.slice(0, 119)}…` : firstBlock;
  }
  return raw.length > 120 ? `${raw.slice(0, 119)}…` : raw;
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
  const chain = (task.meta && typeof task.meta === "object" ? task.meta.tool_chain : null) || {};
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
  const durationStart = task.started_at || task.created_at;
  const durationEnd = task.ended_at || task.completed_at || task.updated_at;
  const duration = formatDuration(
    durationStart,
    isActive ? new Date().toISOString() : durationEnd,
  );
  const durationHtml = isActive && durationStart
    ? `<span data-live-duration-start="${escapeHtml(durationStart)}">${escapeHtml(duration)}</span>`
    : escapeHtml(duration);
  const summaryHtml = [
    escapeHtml(`${toolCount} 次工具调用`),
    durationHtml,
    escapeHtml(isActive ? "运行中" : task.status),
  ]
    .filter(Boolean)
    .join(" · ");

  return `
    <details class="trace-panel" data-thinking-key="${escapeHtml(getThinkingKey(task))}" ${getThinkingOpenAttribute(task, isActive)}>
      <summary class="trace-summary">
        <span class="trace-caret">▸</span>
        <span>${summaryHtml}</span>
      </summary>
      ${body}
    </details>
  `;
}

function renderToolChain(task) {
  const node = $("#toolChain");
  if (!node) return;
  const chain = task?.meta?.tool_chain || {};
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
  const agent = getSelectedAgent();
  const runs = getSelectedRuns();
  const task = getViewRun();

  const selector = $("#runSelector");
  const selectorWrap = $("#runSelectorWrap");
  if (selector && selectorWrap) {
    if (agent && runs.length > 0) {
      selectorWrap.classList.remove("hidden");
      selector.innerHTML = runs
        .map(
          (run) => `
            <option value="${escapeHtml(run.task_id)}" ${run.task_id === task?.task_id ? "selected" : ""}>
              ${escapeHtml(formatTime(run.created_at))} · ${escapeHtml(run.status)}
            </option>
          `,
        )
        .join("");
    } else {
      selectorWrap.classList.add("hidden");
      selector.innerHTML = "";
    }
  }

  if (!agent || !task) {
    $("#inspectorTitle").textContent = displayAgentTitle(agent) || "未选择";
    $("#taskFacts").innerHTML = agent
      ? `
        <dt>Agent</dt><dd>${escapeHtml(displayAgentTitle(agent))}</dd>
        <dt>来源 UMO</dt><dd>${escapeHtml(agent.unified_msg_origin)}</dd>
        <dt>Run 数</dt><dd>${escapeHtml(agent.run_count ?? runs.length)}</dd>
        <dt>任务时间</dt><dd>${escapeHtml(formatTime(agent.last_run_at || agent.updated_at))}</dd>
      `
      : "";
    $("#rawRequest").textContent = "";
    $("#rawMeta").textContent = "";
    if ($("#stopButton")) $("#stopButton").disabled = true;
    renderToolChain(null);
    return;
  }

  $("#inspectorTitle").textContent = compactId(task.task_id);
  const taskTimeLabel = task.ended_at ? "完成时间" : "更新时间";
  const taskTime = task.ended_at || task.updated_at;
  $("#taskFacts").innerHTML = `
    <dt>状态</dt><dd>${escapeHtml(task.status)}</dd>
    <dt>类型</dt><dd>run</dd>
    <dt>Agent</dt><dd>${escapeHtml(displayAgentTitle(agent))}</dd>
    <dt>模式</dt><dd>${escapeHtml(task.mode || "")}</dd>
    <dt>通知</dt><dd>${escapeHtml(task.notification?.notification_id || "-")}</dd>
    <dt>来源 UMO</dt><dd>${escapeHtml(task.unified_msg_origin || agent.unified_msg_origin)}</dd>
    <dt>${taskTimeLabel}</dt><dd>${escapeHtml(formatTime(taskTime))}</dd>
  `;
  if ($("#stopButton")) {
    $("#stopButton").disabled = !["starting", "running"].includes(task.status);
  }

  $("#rawRequest").textContent = task.request_text || "";
  $("#rawMeta").textContent = JSON.stringify({ agent, run: task }, null, 2);

  const trace = state.transcriptRunTrace[task.task_id];
  if (trace) {
    renderToolChain({
      task_id: task.task_id,
      status: task.status,
      meta: { tool_chain: trace },
      started_at: task.started_at || task.created_at,
      ended_at: task.ended_at || task.updated_at,
      updated_at: task.updated_at,
    });
  } else {
    renderToolChain(null);
    loadViewRunTrace(task.task_id);
  }
}

async function loadViewRunTrace(taskId) {
  if (!state.selectedAgentId || !taskId) return;
  if (state.transcriptRunTrace[taskId]) return;
  state.transcriptRunTrace[taskId] = null; // in-flight 占位，防重复拉取
  try {
    const data = await apiGet(
      `console/agents/${encodeURIComponent(state.selectedAgentId)}/runs/${encodeURIComponent(taskId)}/trace`,
    );
    state.transcriptRunTrace[taskId] = data.tool_chain || { entries: [], messages: [] };
    renderInspector();
  } catch {
    delete state.transcriptRunTrace[taskId];
  }
}

async function submitPrompt() {
  const input = $("#promptText");
  const text = input?.value.trim() || "";
  if (!text && state.attachments.length === 0) return;

  const agent = state.dispatchAgent || getConfiguredDefaultAgent();
  const selectedAgent = getSelectedAgent();
  const umo = selectedAgent?.unified_msg_origin || state.selectedUmo;
  if (!umo) {
    toast("先选择或填写 UMO");
    $("#umoModal")?.classList.remove("hidden");
    return;
  }

  const btn = $("#sendBtn");
  if (btn) btn.disabled = true;

  try {
    const imageUrlsRaw = await readyAttachmentPaths();
    const requestText = text || "请查看并处理附带的图片。";

    if (selectedAgent && selectedAgent.active_task_id) {
      if (imageUrlsRaw.length) {
        throw new Error("运行中的 steer 目前只支持文字；请等待完成后用 Resume 附图。");
      }
      await apiPost("console/actions/steer", {
        agent_id: selectedAgent.agent_id,
        task_id: selectedAgent.active_task_id,
        message_text: requestText,
      });
      clearPromptComposer(input);
      toast("已 steer 当前 Agent");
      await loadAgentTranscript(selectedAgent.agent_id, { scrollMode: "bottom" });
      return;
    }

    if (selectedAgent) {
      const res = await apiPost("console/actions/resume", {
        agent_id: selectedAgent.agent_id,
        request_text: requestText,
        unified_msg_origin: umo,
        image_urls_raw: imageUrlsRaw,
      });
      if (!res.outcome) throw new Error(res.error || "resume 失败");
      clearPromptComposer(input);
      await refreshConsole({ silent: true, keepSession: true });
      await loadAgent(res.outcome.agent_id || selectedAgent.agent_id, { scrollMode: "bottom" });
      toast("已 Resume Agent");
      return;
    }

    const res = await apiPost("console/actions/dispatch", {
      unified_msg_origin: umo,
      agent_name: agent,
      request_text: requestText,
      run_in_background: true,
      image_urls_raw: imageUrlsRaw,
    });
    if (!res.outcome) throw new Error(res.error || "派发失败");
    clearPromptComposer(input);
    await refreshConsole({ silent: true, keepSession: false });
    await loadAgent(res.outcome.agent_id, { scrollMode: "bottom" });
    toast("已创建 Agent Run");
  } catch (err) {
    toast(err.message || "发送失败");
  } finally {
    if (btn) btn.disabled = false;
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
    if (state.selectedAgentId === agentId) await loadAgent("");
    toast("Agent 及其所有 Run 已删除");
    await refreshConsole({ silent: true, keepSession: false });
  } catch (err) {
    toast(err.message || "删除 Agent 失败");
  }
}

function requestDeleteRuntimeAgent(agentId) {
  const agent = state.runtimeAgents.find((item) => item.agent_id === agentId);
  if (!agent) return;
  const hasActiveRun =
    Boolean(agent.active_task_id) || ACTIVE_STATUSES.has(agent.last_status || "");
  if (hasActiveRun) {
    toast("Agent 仍有活跃 Run，请先停止");
    return;
  }
  if (agent.pending_notification) {
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

function buildRuntimeMenuItems(agent) {
  const hasActiveRun =
    Boolean(agent.active_task_id) || ACTIVE_STATUSES.has(agent.last_status || "");
  const hasPendingNotification = Boolean(agent.pending_notification);
  const items = [];
  if (hasActiveRun && agent.active_task_id) {
    items.push({ action: "stop", label: "停止当前 Run", taskId: agent.active_task_id });
  }
  if (agent.last_task_id) {
    items.push({ action: "result", label: "读取最新结果", taskId: agent.last_task_id });
  }
  items.push({
    action: "delete-agent",
    label: state.pendingDeleteAgentId === agent.agent_id ? "确认删除 Agent" : "删除 Agent…",
    danger: true,
    disabled: hasActiveRun || hasPendingNotification,
    hint: hasActiveRun
      ? "Agent 仍在运行"
      : hasPendingNotification
        ? "请先读取待通知结果"
        : "",
  });
  return items;
}

function closeSessionMenu() {
  $("#sessionMenu")?.classList.add("hidden");
  $$(".session-item.menu-open").forEach((item) => item.classList.remove("menu-open"));
}

function openSessionMenu(anchor, itemEl, context) {
  const menu = $("#sessionMenu");
  if (!menu) return;
  const agent = state.runtimeAgents.find((item) => item.agent_id === context.agentId);
  if (!agent) return;
  const items = buildRuntimeMenuItems(agent);
  menu.innerHTML = items
    .map(
      (item) => `
        <button class="menu-item ${item.danger ? "danger" : ""}" type="button"
          data-action="${escapeHtml(item.action)}" data-task-id="${escapeHtml(item.taskId || "")}"
          ${item.disabled ? "disabled" : ""}
          ${item.hint ? `title="${escapeHtml(item.hint)}"` : ""}>
          ${escapeHtml(item.label)}
        </button>
      `,
    )
    .join("");
  menu.dataset.kind = context.kind;
  menu.dataset.agentId = context.agentId || "";
  menu.classList.remove("hidden");
  itemEl?.classList.add("menu-open");
  const rect = anchor.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - menuRect.height - 8))}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - menuRect.width - 8))}px`;
}

function closeMobileSidebar() {
  $("#paneLeft")?.classList.remove("mobile-open");
  $("#sidebarScrim")?.classList.remove("show");
}

async function stopCurrentTask() {
  const task = getViewRun();
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
  const agent = getSelectedAgent();
  const task = getViewRun();
  if (!agent || !task) return;
  try {
    const res = await apiPost("console/actions/result", {
      task_id: task.task_id,
      agent_id: agent.agent_id,
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
  const task = getViewRun();
  if (!task) return;
  try {
    const res = await apiPost("console/actions/rerun", { task_id: task.task_id });
    if (!res.outcome) throw new Error(res.error || "重跑失败");
    await refreshConsole({ silent: true, keepSession: false });
    await loadAgent(res.outcome.agent_id, { scrollMode: "bottom" });
    toast("已重新派发");
  } catch (err) {
    toast(err.message || "重跑失败");
  }
}

async function exportHistory() {
  if (!state.selectedAgentId) return;
  try {
    await ensureBridge().download(
      `console/agents/${encodeURIComponent(state.selectedAgentId)}/export`,
      {},
      `maid-agent-${state.selectedAgentId.slice(0, 8)}-transcript.json`,
    );
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

  if (data.type === "runtime_title" && data.agent_id) {
    await loadRuntimeAgents({ force: true });
    if (state.selectedAgentId === data.agent_id) {
      renderChatHeader();
      renderInspector();
    }
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
    if (state.selectedAgentId === data.agent_id) {
      const segments = state.agentTranscript?.runs || [];
      const segment = segments.find((item) => item.task_id === data.task_id);
      if (segment && data.tool_chain) {
        segment.tool_chain = data.tool_chain;
      }
      state.transcriptRunTrace[data.task_id] = data.tool_chain || null;
      const feed = $("#chatFeed");
      const autoScroll = isAtBottom(feed);
      renderChatFeed();
      if (autoScroll) scrollToBottom(feed);
      renderInspector();
      updateSessionList();
    }
    return;
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
    setDocsMode(false);
    loadAgent("");
  });

  // 文档视图切换
  function setDocsMode(on) {
    const docs = $("#docsView");
    const feed = $("#chatFeed");
    const footer = document.querySelector(".chat-footer");
    if (docs) docs.hidden = !on;
    if (feed) feed.hidden = on;
    if (footer) footer.hidden = on;
    const title = $("#chatTitle");
    if (title && on) title.textContent = "使用文档";
    const btn = $("#docsViewButton");
    if (btn) btn.classList.toggle("active", on);
    window.__docsMode = on;
  }
  window.setDocsMode = setDocsMode;

  $("#docsViewButton")?.addEventListener("click", () => {
    setDocsMode(!window.__docsMode);
  });

  const sessionList = $("#sessionList");
  sessionList?.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const item = target.closest(".session-item");
    if (!item) return;
    const agentId = item.dataset.id;
    const menuButton = target.closest("[data-menu-kind]");

    if (menuButton) {
      event.stopPropagation();
      const alreadyOpen = !$("#sessionMenu")?.classList.contains("hidden");
      closeSessionMenu();
      if (alreadyOpen && item.classList.contains("menu-open")) return;
      openSessionMenu(menuButton, item, {
        kind: menuButton.dataset.menuKind,
        agentId,
      });
      return;
    }

    closeSessionMenu();
    closeMobileSidebar();
    await loadAgent(agentId);
  });

  $("#sessionMenu")?.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target?.closest(".menu-item");
    if (!button || button.disabled) return;
    const menu = $("#sessionMenu");
    const action = button.dataset.action;
    const taskId = button.dataset.taskId || "";
    const agentId = menu?.dataset.agentId || "";
    closeSessionMenu();
    if (action === "stop" && taskId) await stopRuntimeRun(taskId);
    if (action === "result" && taskId) await readRuntimeResult(agentId, taskId);
    if (action === "delete-agent") requestDeleteRuntimeAgent(agentId);
  });

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest("#sessionMenu") || event.target.closest("[data-menu-kind]")) return;
    closeSessionMenu();
    if (
      event.target.closest("#agentMenu") ||
      event.target.closest("#dispatchAgentBtn")
    ) {
      return;
    }
    closeAgentMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeSessionMenu();
    closeAgentMenu();
    const panel = $("#paneRight");
    if (panel?.classList.contains("open")) {
      panel.classList.remove("open");
      panel.setAttribute("aria-hidden", "true");
    }
    closeMobileSidebar();
  });

  $("#dispatchAgentBtn")?.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleAgentMenu();
  });

  $("#agentMenu")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const item = target?.closest("[data-agent-name]");
    if (!item || item.disabled) return;
    state.dispatchAgent = item.dataset.agentName || getConfiguredDefaultAgent();
    syncDispatchAgentLabel();
    closeAgentMenu();
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
    loadAgent("");
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
    loadAgent("");
  });

  const promptText = $("#promptText");
  $("#attachButton")?.addEventListener("click", () => $("#imageInput")?.click());
  $("#imageInput")?.addEventListener("change", (event) => {
    addImageFiles(event.target.files);
    event.target.value = "";
  });
  $("#attachmentStrip")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-attachment]");
    if (button) removeAttachment(button.dataset.removeAttachment);
  });
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
    const panel = $("#paneRight");
    if (!panel) return;
    const open = panel.classList.toggle("open");
    panel.setAttribute("aria-hidden", open ? "false" : "true");
  });

  $("#closeDetail")?.addEventListener("click", () => {
    const panel = $("#paneRight");
    if (!panel) return;
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  });

  $("#collapseSidebar")?.addEventListener("click", () => {
    const collapsed = $("#paneLeft")?.classList.toggle("collapsed");
    try {
      localStorage.setItem("maid_console_sidebar_collapsed", collapsed ? "1" : "");
    } catch {
      /* storage unavailable in sandboxed iframe: state is session-only */
    }
  });

  $("#runSelector")?.addEventListener("change", (event) => {
    state.selectedViewRunId = event.target.value || "";
    renderInspector();
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

  $("#settingsBtn")?.addEventListener("click", async () => {
    try {
      // silent poll 不会写表单 DOM；打开时再从内存/接口回填。
      if (state.settings) {
        fillSettings(state.settings);
      } else {
        await loadOverview({ applyForm: true });
      }
    } catch (err) {
      toast(err.message || "加载配置失败");
    }
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
    $("#paneLeft")?.classList.add("mobile-open");
    $("#sidebarScrim")?.classList.add("show");
  });

  $("#sidebarScrim")?.addEventListener("click", closeMobileSidebar);

  window.addEventListener("beforeunload", () => {
    if (state.subscriptionId && bridge?.unsubscribeSSE) {
      bridge.unsubscribeSSE(state.subscriptionId);
    }
    window.clearInterval(state.pollTimer);
    window.clearInterval(state.durationTimer);
  });
}

async function boot() {
  bindEvents();
  try {
    if (localStorage.getItem("maid_console_sidebar_collapsed") === "1") {
      $("#paneLeft")?.classList.add("collapsed");
    }
  } catch {
    /* storage unavailable in sandboxed iframe */
  }
  bridge = window.AstrBotPluginPage || null;
  if (!bridge) {
    setStreamState("signal-error", "桥接缺失");
    toast("页面桥接未加载");
    return;
  }

  await bridge.ready();
  state.dispatchAgent = getConfiguredDefaultAgent();
  syncDispatchAgentLabel();
  setStreamState("signal-poll", "轮询同步");
  startPolling();
  startDurationTicker();
  await refreshConsole({ silent: true, keepSession: false });
  // silent 刷新不写表单，启动后用已加载的 settings 回填一次。
  if (state.settings) fillSettings(state.settings);
  await subscribeStream();
}

boot().catch((err) => {
  setStreamState("signal-error", "启动失败");
  toast(err.message || "启动失败");
});
