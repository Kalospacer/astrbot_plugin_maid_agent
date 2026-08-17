import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
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

/**
 * 去掉 Vite 默认加的 crossorigin：插件页跑在 dashboard 的 iframe 里，
 * crossorigin 会把脚本/样式请求切成 CORS 模式，而这些资源走的是插件页自己的
 * 鉴权路由，没有 CORS 响应头。同源加载不需要这个属性。
 */
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
 * KaTeX 字体不进产物：CSS 里的 url 指向 assets/fonts/<原名>.woff2，
 * 由后端启动时从 AstrBot dashboard dist 里物化同名文件（dashboard 自带
 * katex，字体同源复用，插件 zip 不再背 20 个字体外链文件）。
 * 三个格式只留 woff2 引用 —— @font-face 按顺序用第一个支持的格式，
 * 现代浏览器全支持 woff2，woff/ttf 是永远不会被请求的回退。
 */
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
  // 显式钉死 root，这样从仓库任何目录起 vite 都指向 webui/ 而不是 process.cwd()
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [react(), injectBridgeSdk(), stripCrossorigin(), katexFontsExternal()],
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
        // KaTeX 字体保留原名进 assets/fonts/（构建插件随后会把字体文件本身
        // 从产物里删掉，CSS 里留下这个稳定路径给后端物化）；其余资源照旧打平。
        assetFileNames: (assetInfo) =>
          /\.(woff2|woff|ttf)$/.test(assetInfo.name ?? "")
            ? "assets/fonts/[name][extname]"
            : "assets/console[extname]",
      },
    },
  },
});
