import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";
import { boot, shutdown } from "@/store/app";
import { hasBridge, ready } from "@/api/bridge";
import "@/styles/app.css";

async function main() {
  const root = createRoot(document.getElementById("root")!);
  if (!hasBridge()) {
    if (import.meta.env.DEV) {
      await import("@/../dev/mock-bridge.ts");
    }
  }
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  try {
    if (hasBridge()) await ready();
    await boot();
  } catch (error) {
    console.error("console boot failed", error);
  }
}

void main();

window.addEventListener("beforeunload", () => {
  void shutdown();
});
