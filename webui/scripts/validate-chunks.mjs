// 守护宿主约束（AstrBot plugin_page_service）：产物必须是零相对导入的单文件，
// 且体积不能失控。
//
// 旧版本校验的是「动态 import 引用的 chunk 是否存在」——但 vite.config.ts 强制
// inlineDynamicImports，产物里永远是 0 个动态导入，那个门禁从来没有真正跑过。
//
// 真正会出事的是反过来：一旦有人拆出了 chunk，60s 的 asset_token 过期后运行时
// 再去加载它就是 401；而 minify 后的 from"./x.js" 也不匹配宿主的 URL 改写正则。
// 所以这里断言「不许出现相对导入」，并给体积上一道回归闸。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const assetsDir = fileURLToPath(new URL("../../pages/console/assets/", import.meta.url));

// 语法表体积容易在加一门语言时悄悄翻倍（ruby 单文件 52 KB，闭包 1961 KB），
// 这个上限是拿来在 CI 里绊一下的，不是精确目标。
const MAX_JS_KB = 1600;

const failures = [];
const files = readdirSync(assetsDir);
const entry = readFileSync(join(assetsDir, "console.js"), "utf8");

// 1) 单文件：assets/ 下不该有第二个 .js
const jsFiles = files.filter((f) => f.endsWith(".js"));
if (jsFiles.length !== 1) {
  failures.push(`产物应为单个 .js，实际 ${jsFiles.length} 个: ${jsFiles.join(", ")}`);
}

// 2) 零相对导入：静态 import/export-from 与动态 import() 都不许指向 ./
const relativePatterns = [
  [/import\(\s*["'`]\.\//g, "动态 import('./…')"],
  [/\bfrom\s*["']\.\//g, "静态 from './…'"],
  [/\bimport\s*["']\.\//g, "副作用 import './…'"],
];
for (const [pattern, label] of relativePatterns) {
  const hits = entry.match(pattern);
  if (hits !== null) failures.push(`${label} 出现 ${hits.length} 次——宿主 token 会 401`);
}

// 3) 体积闸
const jsKb = Math.round(statSync(join(assetsDir, "console.js")).size / 1024);
if (jsKb > MAX_JS_KB) {
  failures.push(`console.js ${jsKb} KB 超过上限 ${MAX_JS_KB} KB（多半是又拖进了 shiki 语法）`);
}

// 4) 语法表规模：顶层 Object.freeze(JSON.parse(...)) 是首屏同步解析的成本
const grammarCount = (entry.match(/Object\.freeze\(JSON\.parse\(/g) ?? []).length;

console.log(`单文件核对: ${jsFiles.length} 个 .js, ${jsKb} KB`);
console.log(`相对导入核对: ${failures.some((f) => f.includes("./")) ? "失败" : "0 个"}`);
console.log(`内联语法数: ${grammarCount}（首屏同步 JSON.parse）`);

if (failures.length > 0) {
  for (const line of failures) console.log(`FAIL ${line}`);
  process.exit(1);
}
console.log("OK");
