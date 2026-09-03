<div align="center"><h1>AstrBot Plugin Maid Agent</h1><i>—— 代理女仆 ——</i></div>

将角色扮演主模型与工具执行子代理分离。主模型通过明确的 maid 控制面创建独立 subagent；子代理有私有 transcript 和工具上下文，不会写入主模型的 LLM history。

## 前提与配置

- AstrBot `>=4.20.0`。没有配置任何 SubAgent 时，插件会自动创建一个默认管家 subagent（名称取 `default_agent_name`，默认 `butler`，使用全部可用工具，人格与调度模板同源）。
- `allowed_agent_names` 包含可调度的 SubAgent 名称，不能为空。每次任务仍须显式指定名称；名称不在白名单或没有对应 subagent 时，任务回退到 `default_agent_name`（默认 `butler`）。
- 保存配置时严格校验：类型、范围、名单或提示模板错误会被整单拒绝，并返回字段级错误，不会被静默修复。
- 加载存量配置时容错：旧版本遗留的配置键会被忽略，无效的存量值回退到该键默认值并记录警告，不会导致插件无法加载。首次成功保存后，存储中的配置会被规范化为当前配置项。

## 主模型工具

当 `hide_native_tools=true`（默认）时，主模型**仅**看到以下五个工具：

| 工具 | 用途 |
| --- | --- |
| `maid_agent` | 创建一个任务或最多五个批量任务。参数为 `prompt`、`subagent_type`、`run_in_background`、可选 `resume_agent_id` 或 `tasks`。 |
| `maid_send_message` | 向正在运行的 Agent 发送补充消息：`agent_id`、`message`。 |
| `maid_list_agents` | 列出当前聊天来源创建的 Agent、任务和执行状态。 |
| `maid_task_output` | 读取任务结果：`task_id`、可选 `block` 与 `timeout_ms`。 |
| `maid_task_stop` | 请求停止任务：`task_id`。 |

`hide_native_tools=false` 时保留 AstrBot 原有的主模型工具暴露策略；`hide_transfer_tools` 仍只负责在该模式下隐藏 `transfer_to_*`。这两个可见性选项不会改变任务在哪个环境执行。

## 执行环境

`dispatch_session_mode` 只影响聊天中由 `maid_agent` 新建的任务：

- `foreground`：使用触发任务的真实 AstrBot event/session，因此子代理的图片、文件、富媒体和第三方工具能力按聊天平台原样生效。主模型 history 仍保持隔离。每个 UMO 同时最多一个 foreground 子代理；批量任务按输入顺序保留前台名额，其余自动进入 background。
- `background`（默认）：使用 `DashboardMaidEvent` harness 隔离沙箱，用户侧静默运行；完成后只把一次结构化摘要回传主模型。

任务在开始时就固定环境，绝不会因等待时间而中途切换。控制台不具备真实聊天 event，所以它创建的任务始终是隔离 sandbox，并在界面中明确标注。

## Console 与运行轨迹

插件 Console 可创建、停止、续接和查看子代理。创建任务前必须选择 Agent。会话概要和事件流会显示执行环境、来源、`dispatch_id`、`agent_id`、`task_id`、自动转后台原因、前台 lease 与投递状态；不会持久化真实 event、附件对象或敏感 extras。

## 配置参考

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `allowed_agent_names` | `[butler]` | 可调度的 SubAgent 白名单，不能为空。 |
| `default_agent_name` | `butler` | 名称不匹配时回退的默认 Agent，须在白名单内；没有任何 subagent 时会自动以此名创建。 |
| `hide_native_tools` | `true` | 开启时主模型只暴露五个 maid 工具。 |
| `hide_transfer_tools` | `true` | native 工具可见时隐藏 `transfer_to_*`。 |
| `dispatch_session_mode` | `background` | 聊天派发使用真实 event 或隔离 sandbox。 |
| `include_raw_user_input` | `true` | 将用户原话加入子代理 prompt。 |
| `log_raw_llm_io` | `false` | DEBUG 记录完整 LLM I/O，可能含敏感数据。 |
| `dispatch_prompt_template` | 内置 | 只允许 `{user_input_block}`、`{maid_request_block}` 占位符。 |
| `memory_agent_names` | `[]` | 启用 Agent 私有记忆的白名单子集。 |
| `max_active_per_umo` / `max_active_global` | `5` / `20` | 活跃任务并发上限。 |
| `retention_days` / `max_turn_seconds` | `30` / `1800` | 轨迹保留与单 turn 看门狗。 |
