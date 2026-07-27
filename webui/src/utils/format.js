/** 时间 / 体积 / 文本裁剪等展示格式化。从 1.4.1 的 pages/console/app.js 移植。 */

export function compactId(value) {
  const text = String(value || "");
  return text.length > 12 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

export function formatTime(value) {
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

export function formatClock(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const ms = new Date(endIso) - new Date(startIso);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

export function formatFileSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * 派发给管家的请求文本里带着【大小姐请求】/【对方原话】这类结构化标记，
 * 展示时只取第一段正文并截断。
 */
export function displayUserText(text, max = 400) {
  const raw = String(text ?? "").trim();
  if (!raw) return "";
  const blockMatch = raw.match(/【(?:大小姐请求|对方原话)】\s*\n?/);
  if (blockMatch) {
    const after = raw.slice(blockMatch.index + blockMatch[0].length).trim();
    const firstBlock = after.split(/【[^】]+】/)[0].trim();
    if (firstBlock) return firstBlock.length > max ? `${firstBlock.slice(0, max - 1)}…` : firstBlock;
  }
  return raw.length > max ? `${raw.slice(0, max - 1)}…` : raw;
}
