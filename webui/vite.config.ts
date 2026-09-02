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

function katexFontsExternal() {
  return {
    name: "maid-console-katex-fonts-external",
    generateBundle(_options: unknown, bundle: Record<string, { fileName: string }>) {
      for (const key of Object.keys(bundle)) {
        if (/assets\/fonts\/.*\.(woff2|woff|ttf)$/.test(key)) delete bundle[key];
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
    target: "es2020",
    // 宿主约束（AstrBot plugin_page_service）：所有资产经 /api/plugin/page/content/
    // 以 60s JWT asset_token 签名下发，URL 改写基于正则——minify 后的静态
    // from"./x.js" 无空白不匹配改写规则，且 token 60s 过期后运行时再加载 chunk 会 401。
    // 因此产物必须是零相对导入的单文件，拆包在本宿主下行不通。
    cssCodeSplit: false,
    modulePreload: false,
    assetsInlineLimit: 0,
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
