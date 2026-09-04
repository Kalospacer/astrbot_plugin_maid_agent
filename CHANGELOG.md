# Changelog

## 2.0.6 - 2026-09-04

破坏性改动：删掉前台/后台执行模式，派发一律非阻塞，管家干活时会自己在聊天里说话。

起因是前台模式把「用真实聊天 event」和「阻塞等结果」绑在了同一个开关上。阻塞那半边会把主 agent 的 run 一直挂在工具调用里，AstrBot 于是把用户随后发的消息当成 follow-up 吞进 tool result（`[SYSTEM NOTICE] User sent follow-up messages...`），用户没法在管家干活的同时继续聊天。这两件事本来正交，这个版本把它们拆开。

### 破坏性

- **删除 `dispatch_session_mode` 配置项**。聊天派发不再有执行环境之分。旧配置里残留这个键会被加载时忽略，不影响启动。
- **`maid_agent` 删除 `run_in_background` 参数**（含 `tasks[]` 里那一份）。派发永远立即返回 `{agent_id, task_id, dispatch_id}` 句柄。
- **`maid_task_output` 删除 `block` / `timeout_ms` 参数**。它曾经能阻塞最长 600 秒，是第二条把主 agent 钉死的路径；现在只读一眼，运行中返回进度，跑完返回终态。
- 两个工具的 description 重写为显式契约：`maid_agent` 只入队、毫秒返回、收到后必须停止调工具并在本轮回话；`maid_task_output` 是一次性快照而非等待原语。返回值另加 `next` 字段，在决策点直接给出下一步。不写清楚的话主模型会自己轮询 `maid_task_output` 把 run 吊着——阻塞的单位是整个 agent run，不是单次工具调用。
- 会话元数据与 host 帧不再携带 `executionMode` / `backgroundReason` / `foregroundLease`，前端类型同步移除。

### 新增

- **管家会在聊天里说话**：干活过程中每一段自然语言正文以 `管家名: ...` 发到聊天。最后一段押后不发——那是任务汇报，随终态回灌给大小姐转述，避免同一份结论出现两遍。
- **派发时快照真实 event**：图片复制进会话附件区，发送者、昵称、群、self_id、平台身份与能力位一并带走。管家不再持有主流程的 event，因此主流程结束清理临时文件不再影响它。合成事件的 `send` 改为按 UMO 走平台适配器真实投递。
- **`/maid status` 与 `maid_task_output` 有内容了**：管家名、agent 短 ID、已跑秒数、第几步、当前/刚用完的工具**及其入参与输出**、最近一段正文（没有正文时退回推理）。只给工具名没有信息量——女仆大量时间待在只发工具调用、不说话的步里。数据全部从事件日志尾部现读，不额外记账。工具入参用 `json.loads(strict=False)` 解析：模型常把裸换行写进字符串，严格模式会让整块参数退化成 JSON 噪音，而那一坨正是要看的代码本身。

### 修复

- **管家 `send_message_to_user` 的输出进黑洞**：合成事件的 `send` 只往一个没人读的 list 里 append，管家主动发的每条消息都被静默丢弃。现在真实投递。
- **`fg_timeout` 读错配置键**：`_main_tool_call_timeout` 读的是 `provider_settings.tool_call_timeout`，而 Core 读的是 `agent_runner.config.misc.tool_call_timeout`，前者不存在，于是永远取写死的 120。随前台等待一并删除。
- **被 Core 掐断时结果静默丢失**：前台等待只接 `asyncio.TimeoutError`，Core 超时取消抛的是 `CancelledError`，接不住就跳过了补 `notify=True` 的那段，会话保持 `notify=False` / `deliveryStatus=skipped`，管家跑完 `_on_turn_terminal` 直接 return，结果烂在日志里。随前台等待一并删除。
- **通知回灌不排队**：`_notify_main_agent` 绕开 pipeline 自起主 agent，既不拿 session lock 也不注册 runner。改成非阻塞后每个任务都以它收尾，会插进用户正在进行的一轮，两个 runner 抢写同一份 conversation history。现在包在 `session_lock_manager.acquire_lock(umo)` 里；同时「这份汇报归谁转达」改成 `deliveryClaimed` 原子认领（在 `log.lock` 里读改写），因为通知回灌和 `maid_task_output` 各自持有的外层锁不是同一把。
- **retention 清理每小时报错**：`running` 是 set，却按 dict 调 `running.pop(sid, None)`，`TypeError` 被外层 `except` 吞成一条"清理失败"。该变量本就没用，删掉。

### 不变

- 五工具 API 的其余签名、`/maid stop`、控制台 RPC 协议、SSE 帧格式、插件页集成均不变。
- 控制台派发仍在隔离环境里跑，只是不再叫"background 模式"，也不会往聊天里发消息。
- 并发上限语义不变（`max_active_per_umo` / `max_active_global`），前台租约删除后它是唯一的并发闸门。
- 已有会话数据无需迁移，不修改 AstrBot Core 任何文件。

## 2.0.5 — 2026-09-03

Breaking release. The legacy two-tool control plane and all migration, alias, timeout-switch and automatic-repair paths were removed. Default-Agent fallback returns as an explicit `default_agent_name` setting.

- Added the Claude-style `maid_agent`, `maid_send_message`, `maid_list_agents`, `maid_task_output`, and `maid_task_stop` tools.
- `hide_native_tools=true` now exposes only those five tools; its default and disabled behavior are unchanged.
- Added strict `dispatch_session_mode` with stable `foreground` real-event execution and `background` isolated harness execution.
- Added one foreground lease per UMO, deterministic batch allocation, lifecycle release, delivery tracing, and Console runtime metadata.
- Console configuration now honors schema options, reports strict settings errors, requires a selected Agent, and labels its tasks as isolated sandboxes.
- Loading existing settings is now tolerant: removed legacy keys are ignored and invalid stored values fall back to defaults with a warning, so a pre-2.0.5 config cannot block plugin startup; saving stays strict and persists a normalized settings object.
- Restored `default_agent_name`: an unmatched or disallowed `subagent_type` falls back to it instead of failing; resolution errors now list the allowed agents.
- When no subagent is configured at all, the plugin now provisions one automatically — named after `default_agent_name`, all tools, current-chat provider, persona shared with the dispatch prompt template — instead of leaving dispatch with nothing to resolve.
