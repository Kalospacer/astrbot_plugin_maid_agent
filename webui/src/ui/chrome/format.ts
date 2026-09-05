// 共享时间/时长/token 格式化：对齐 DSH ui-chat message-chrome.ts 与 token-format.ts。
// 所有函数纯化、无 locale 依赖（本插件界面固定中文，直接内嵌 DSH zh 模板）。

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** 秒级用时：「45秒」/「2分42秒」（DSH duration.minutes/seconds 模板）。 */
export function formatRunDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}分${pad2(seconds)}秒` : `${seconds}秒`;
}

/** 紧凑时长：一分钟内 45.2s，之后 2m42s（DSH formatDuration）。 */
export function formatDuration(ms: number): string {
  const s = ms / 1000;
  if (s < 60) return `${Math.round(s * 10) / 10}秒`;
  const whole = Math.round(s);
  return `${Math.floor(whole / 60)}分${whole % 60}秒`;
}

/** 消息时间戳：同日 HH:mm，同年加「M月d日」，跨年加年份（DSH formatMessageClock）。 */
export function formatMessageClock(time: number, now: number = Date.now()): string {
  const d = new Date(time);
  const n = new Date(now);
  const clock = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (
    d.getFullYear() === n.getFullYear() &&
    d.getMonth() === n.getMonth() &&
    d.getDate() === n.getDate()
  ) {
    return clock;
  }
  const md =
    d.getFullYear() === n.getFullYear()
      ? `${d.getMonth() + 1}月${d.getDate()}日`
      : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  return `${md} ${clock}`;
}

/** 解码吞吐：「103 tok/s」（DSH stats.tokensPerSecond 模板）。 */
export function formatThroughput(tokensPerSecond: number): string {
  const rounded = tokensPerSecond >= 100
    ? Math.round(tokensPerSecond)
    : Math.round(tokensPerSecond * 10) / 10;
  return `${rounded} tok/s`;
}

/** 紧凑 token 数：517 / 12.2K / 517K / 1.2M（DSH formatTokens）。 */
export function formatTokens(value: number): string {
  const scaled = (candidate: number): string =>
    candidate >= 100 ? String(Math.round(candidate)) : String(Math.round(candidate * 10) / 10);
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${scaled(value / 1_000)}K`;
  return `${scaled(value / 1_000_000)}M`;
}

/** 精确 token 数（千分位逗号分组，DSH formatExactTokens）。 */
export function formatExactTokens(value: number): string {
  const digits = String(value);
  const groups: string[] = [];
  for (let end = digits.length; end > 0; end -= 3) {
    groups.unshift(digits.slice(Math.max(0, end - 3), end));
  }
  return groups.join(",");
}

/** 四舍五入到指定精度的百分比单元（原 DSH roundedPercentUnits：二分避免浮点漂移）。 */
function roundedPercentUnits(cacheReadTokens: number, denominator: number, decimalPlaces: 0 | 1): number {
  const unitsPerPercent = decimalPlaces === 0 ? 1 : 10;
  const scale = unitsPerPercent * 100;
  const doubledScale = scale * 2;
  const denominatorQuotient = Math.floor(denominator / doubledScale);
  const denominatorRemainder = denominator % doubledScale;
  let lower = 0;
  let upper = scale;
  while (lower < upper) {
    const candidate = Math.floor((lower + upper + 1) / 2);
    const factor = candidate * 2 - 1;
    const threshold = factor * denominatorQuotient + Math.ceil((factor * denominatorRemainder) / doubledScale);
    if (cacheReadTokens >= threshold) lower = candidate;
    else upper = candidate - 1;
  }
  return lower;
}

function displayPercentUnits(units: number, decimalPlaces: 0 | 1): string {
  if (decimalPlaces === 0) return String(units);
  const whole = Math.floor(units / 10);
  const tenths = units % 10;
  return tenths === 0 ? String(whole) : `${whole}.${tenths}`;
}

/**
 * 缓存命中百分比：部分命中不允许舍入到 100%（DSH formatCacheHitPercent 原实现）。
 * decimalPlaces 默认 0（整数）；会舍到 100 的部分命中自动降到 99.9x 形式。
 */
export function formatCacheHitPercent(
  cacheReadTokens: number,
  promptTokens: number,
  decimalPlaces: 0 | 1 = 0,
): string | null {
  if (promptTokens === 0) return null;
  const missedInputTokens = promptTokens - cacheReadTokens;
  if (missedInputTokens === 0) return "100";

  const roundedUnits = roundedPercentUnits(cacheReadTokens, promptTokens, decimalPlaces);
  const fullHitUnits = decimalPlaces === 0 ? 100 : 1_000;
  if (roundedUnits < fullHitUnits) return displayPercentUnits(roundedUnits, decimalPlaces);

  let distinguishingPlaces = 1;
  let scaledDoubleGap = missedInputTokens * 200;
  const denominatorTens = Math.floor(promptTokens / 10);
  while (scaledDoubleGap <= denominatorTens) {
    scaledDoubleGap *= 10;
    distinguishingPlaces += 1;
  }
  const denominatorOnes = promptTokens % 10;
  let roundedLoss = 5;
  for (let loss = 1; loss < 5; loss += 1) {
    const factor = loss * 2 + 1;
    const threshold = factor * denominatorTens + Math.floor((factor * denominatorOnes) / 10);
    if (scaledDoubleGap <= threshold) {
      roundedLoss = loss;
      break;
    }
  }
  return `99.${"9".repeat(distinguishingPlaces - 1)}${10 - roundedLoss}`;
}
