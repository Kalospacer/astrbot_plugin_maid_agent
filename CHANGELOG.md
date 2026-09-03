# Changelog

## 2.0.5 — 2026-09-03

Breaking release. The legacy two-tool control plane and all migration, alias, default-Agent, timeout-switch and automatic-repair paths were removed.

- Added the Claude-style `maid_agent`, `maid_send_message`, `maid_list_agents`, `maid_task_output`, and `maid_task_stop` tools.
- `hide_native_tools=true` now exposes only those five tools; its default and disabled behavior are unchanged.
- Added strict `dispatch_session_mode` with stable `foreground` real-event execution and `background` isolated harness execution.
- Added one foreground lease per UMO, deterministic batch allocation, lifecycle release, delivery tracing, and Console runtime metadata.
- Console configuration now honors schema options, reports strict settings errors, requires a selected Agent, and labels its tasks as isolated sandboxes.
