import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

const BRIDGE_SDK_SRC = "/api/plugin/page/bridge-sdk.js";

/**
 * AstrBot 的 plugin page 服务在吐出 index.html 时会把 src="/api/plugin/page/bridge-sdk.js"
 * 重写成带鉴权参数的真实地址（plugin_page_service.rewrite_plugin_page_html）。
 * 这个绝对路径不能写进源 index.html —— Vite 会当成 publicDir 资源去解析然后报错，
 * 所以在构建期注入。
 */
function injectBridgeSdk() {
  return {
    name: "maid-console-inject-bridge-sdk",
    transformIndexHtml: {
      order: "post",
      handler(html) {
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

/**
 * 去掉 Vite 默认加的 crossorigin：插件页跑在 dashboard 的 iframe 里，
 * crossorigin 会把脚本/样式请求切成 CORS 模式，而这些资源走的是插件页自己的
 * 鉴权路由，没有 CORS 响应头。同源加载不需要这个属性。
 */
function stripCrossorigin() {
  return {
    name: "maid-console-strip-crossorigin",
    enforce: "post",
    transformIndexHtml(html) {
      return html.replace(/\s+crossorigin(?==|\s|>)/g, "");
    },
  };
}

export default defineConfig({
  // 显式钉死 root，这样从仓库任何目录起 vite 都指向 webui/ 而不是 process.cwd()
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [vue(), injectBridgeSdk(), stripCrossorigin()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    // 产物直接落到插件页目录，需提交进 git：AstrBot 装插件时不会跑 npm。
    outDir: "../pages/console",
    emptyOutDir: true,
    target: "es2020",
    cssCodeSplit: false,
    modulePreload: false,
    assetsInlineLimit: 0,
    rollupOptions: {
      output: {
        // 单包不分块：这样 index.html 里只有两个相对引用需要 AstrBot 重写，
        // 不依赖它较新版本才有的 JS import 说明符重写能力。
        inlineDynamicImports: true,
        entryFileNames: "assets/console.js",
        assetFileNames: "assets/console[extname]",
      },
    },
  },
});
