// 校验构建产物中所有动态导入路径都指向真实存在的 chunk 文件，
// 并通过 vite preview 实际请求每个 chunk，确认按需加载链路完整。
import { spawn } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const PORT = 7138;
const BASE = `http://127.0.0.1:${PORT}/`;
const assetsDir = fileURLToPath(new URL("../../pages/console/assets/", import.meta.url));

// 1) 静态核对：console.js 里出现的相对 import 都有对应文件
const entry = readFileSync(join(assetsDir, "console.js"), "utf8");
const importRefs = new Set(
  [...entry.matchAll(/import\(\s*["']\.\/([^"']+)["']\s*\)/g)].map((m) => m[1]),
);
const files = new Set(readdirSync(assetsDir));
let missing = 0;
for (const ref of importRefs) {
  if (!files.has(ref)) {
    console.log(`FAIL 动态导入引用了不存在的文件: ${ref}`);
    missing++;
  }
}
console.log(`静态核对: ${importRefs.size} 个动态导入, 缺失 ${missing}`);

// 2) HTTP 核对：所有 chunk 都能被 preview 服务
const server = spawn(
  process.execPath,
  ["node_modules/vite/bin/vite.js", "preview", "--port", String(PORT), "--strictPort"],
  { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
);

async function waitReady() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(BASE);
      if (res.ok) return;
    } catch { /* retry */ }
    await delay(250);
  }
  throw new Error("preview server did not start");
}

try {
  await waitReady();
  const chunks = [...files].filter((f) => f.endsWith(".js") || f.endsWith(".css"));
  let failed = 0;
  for (const file of chunks) {
    const res = await fetch(BASE + "assets/" + file);
    if (!res.ok) {
      console.log(`FAIL ${res.status} assets/${file}`);
      failed++;
    }
    await res.arrayBuffer();
  }
  console.log(`HTTP 核对: ${chunks.length} 个资源, 失败 ${failed}`);
  server.kill();
  setTimeout(() => process.exit(missing === 0 && failed === 0 ? 0 : 1), 300);
} catch (error) {
  console.error("ERROR:", error.message);
  server.kill();
  setTimeout(() => process.exit(1), 300);
}
