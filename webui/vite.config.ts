import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const BRIDGE_SDK_SRC = "/api/plugin/page/bridge-sdk.js";

function injectBridgeSdk() {
  return {
    name: "maid-console-inject-bridge-sdk",
    transformIndexHtml: {
      order: "post",
      handler(html: string) {
        return {
          html,
          tags: [
            {
              tag: "script",
              attrs: { src: BRIDGE_SDK_SRC },
              injectTo: "head-prepend",
            },
          ],
        };
      },
    },
  };
}

function stripCrossorigin() {
  return {
    name: "maid-console-strip-crossorigin",
    enforce: "post",
    transformIndexHtml(html: string) {
      return html.replace(/\s+crossorigin(?==|\s|>)/g, "");
    },
  };
}

/**
 * KaTeX 字体不进产物：运行时由 main.py 从 dashboard dist 物化到
 * pages/console/assets/fonts/（见 _materialize_katex_fonts）。
 *
 * 只剔除 KaTeX_ 前缀的文件——早先这里匹配的是 assets/fonts 下的所有字体，
 * 会把插件自带的 Anthropic Sans / Outfit 一并删掉。
 */
function katexFontsExternal() {
  return {
    name: "maid-console-katex-fonts-external",
    generateBundle(_options: unknown, bundle: Record<string, { fileName: string }>) {
      for (const key of Object.keys(bundle)) {
        if (/assets\/fonts\/KaTeX_.*\.(woff2|woff|ttf)$/.test(key)) delete bundle[key];
      }
    },
  };
}

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [react(), injectBridgeSdk(), stripCrossorigin(), katexFontsExternal()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../pages/console",
    emptyOutDir: true,
    target: "es2022",
    // 宿主约束（AstrBot plugin_page_service）：所有资产经 /api/plugin/page/content/
    // 以 60s JWT asset_token 签名下发，URL 改写基于正则——minify 后的静态
    // from"./x.js" 无空白不匹配改写规则，且 token 60s 过期后运行时再加载 chunk 会 401。
    // 因此产物必须是零相对导入的单文件，拆包在本宿主下行不通。
    cssCodeSplit: false,
    modulePreload: false,
    assetsInlineLimit: 0,
    // 单文件是宿主约束不是疏忽，"考虑拆包" 的默认告警在这里只是噪音；
    // 体积回归由 scripts/validate-chunks.mjs 把关。
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        entryFileNames: "assets/console.js",
        assetFileNames: (assetInfo) =>
          /\.(woff2|woff|ttf)$/.test(assetInfo.name ?? "")
            ? "assets/fonts/[name][extname]"
            : "assets/console[extname]",
      },
    },
  },
});
