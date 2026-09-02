// 自包含验证脚本：启动 vite preview → 逐个请求关键资源 → 退出并清理。
// 不依赖后台进程，脚本结束即释放端口。
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const PORT = 7137;
const BASE = `http://127.0.0.1:${PORT}/`;

const server = spawn(
  process.execPath,
  ["node_modules/vite/bin/vite.js", "preview", "--port", String(PORT), "--strictPort"],
  { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
);

let serverLog = "";
server.stdout.on("data", (d) => (serverLog += d));
server.stderr.on("data", (d) => (serverLog += d));

function shutdown(code) {
  server.kill();
  setTimeout(() => process.exit(code), 300);
}

async function waitReady() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(BASE);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await delay(250);
  }
  throw new Error("preview server did not start\n" + serverLog);
}

async function check(path, expect) {
  const res = await fetch(BASE + path);
  const body = await res.text();
  const ok = res.ok && (!expect || body.includes(expect));
  console.log(`${ok ? "PASS" : "FAIL"}  ${res.status}  ${path}  (${body.length} bytes)`);
  return { ok, body };
}

try {
  await waitReady();

  const index = await check("", '<div id="root">');
  const results = [index.ok];

  // 入口脚本与拆出的 chunk
  const entry = await check("assets/console.js", "createRoot");
  results.push(entry.ok);
  results.push((await check("assets/console.css", "--maid-alias-bg-base")).ok);

  // 从 index.html 里解析实际引用的资源，全部请求一遍
  const assetRefs = [...index.body.matchAll(/(?:src|href)="\.\/(assets\/[^"]+)"/g)].map((m) => m[1]);
  for (const ref of assetRefs) {
    results.push((await check(ref)).ok);
  }

  // mock 模式下 dev-only 资源在 preview 不应 404 阻塞入口（bridge sdk 注入只在生产）
  console.log("\nasset refs from index.html:", assetRefs.join(", ") || "(none)");

  shutdown(results.every(Boolean) ? 0 : 1);
} catch (error) {
  console.error("VALIDATION ERROR:", error.message);
  shutdown(1);
}
