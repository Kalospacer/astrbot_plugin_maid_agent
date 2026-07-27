import { createApp } from "vue";

import App from "@/App.vue";
import "@/styles/base.css";

async function bootstrap() {
  // 本地开发：没有 dashboard 的 iframe 桥，用 mock 顶上。
  // import.meta.env.DEV 在生产构建里被替换成 false，整个分支会被摇掉。
  if (import.meta.env.DEV && !window.AstrBotPluginPage) {
    const { installMockBridge } = await import("../dev/mock-bridge.js");
    installMockBridge();
  }
  createApp(App).mount("#app");
}

bootstrap();
