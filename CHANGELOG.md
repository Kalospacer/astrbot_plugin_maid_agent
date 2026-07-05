# Changelog

## 1.1.4 - 2026-07-06

- 新增插件内置 WebUI 控制台：提供 UMO 隔离的会话列表、GPT 风格任务续接输入、右侧 Inspector、历史导出、会话删除/重命名/置顶与全局配置编辑入口。
- 修复 WebUI bridge 初始化与 SSE 订阅路径，前端现在正确等待 `AstrBotPluginPage.ready()`，订阅 `console/stream`，并在 SSE 不可用时使用轮询兜底同步，避免页面卡在“离线/加载中”或发送时报 `apiPost` 未定义。
- 修复自动刷新抢占阅读位置的问题：轮询刷新会保留聊天滚动位置，只有用户已在底部时才自动跟随新输出；同时保留用户手动展开/收起的思考过程状态，避免刷新后自动关闭。
- 恢复并增强完整工具调用链监控：子 agent 执行时持续保存 runner messages 快照，Inspector 新增“调用链”页签，按顺序展示 assistant 输出、工具调用、工具返回和原始 messages。
- 修复 Console 续接运行中任务时被 sender 校验误拦截的问题，Dashboard/Console 创建的后台任务现在允许同一 UMO 继续补充要求。
- 移除 WebUI 中危险的“清空历史”按钮，导出历史保留；停止、结束 Session、重跑等操作改为真实后端动作接口。
- 将 WebUI 任务更新/删除接口改为 POST 路由，适配 AstrBot 插件页 bridge 仅支持 GET/POST 的限制。

## 1.1.3 - 2026-06-07

- 修复流式输出关闭后，后台追答请求（`_request_maid_follow_up`）中因携带结构化 tool 消息历史且未提供 `tools` 定义，导致部分严格的 API 平台（如 Minimax、DeepSeek）返回空输出或报错 `EmptyModelOutputError` 的兼容性问题。此修复通过将追答历史简化为标准文本对话实现 100% 服务端兼容性。
- 修复在流式输出开启时，由于 AstrBot 核心 early-return 导致 `after_message_sent` 钩子失效，后台任务无法被正常唤起的问题。现在通过拦截并包裹 `result.async_stream`，在流式传输结束后自动触发后台调度，实现与流式模式的无缝融合。

## 1.1.2 - 2026-04-29

- 仅在插件侧修复 `call_maid` 工具历史兼容问题，将 `ThinkPart` / `TextPart` 等 Pydantic 内容块递归转为 OpenAI 兼容的普通 dict，避免 AstrBot core 在 `_finally_convert_payload` 中对对象调用 `.get()` 崩溃。
- 为 Kimi / Moonshot thinking 模式补齐 assistant tool-call 历史的 `reasoning_content`，支持从 `LLMResponse.reasoning_content`、`raw_completion.message.reasoning(_content)` 与 `reasoning_details` 兜底提取。
- 安装窄作用 OpenAI provider 兼容补丁，将 Kimi / Moonshot 返回在 `message.reasoning` 的 thinking 内容映射到 `LLMResponse.reasoning_content`，使 core 同轮 tool-loop 二次请求能自动携带 `ThinkPart`。
- 在主模型请求清洗阶段修复旧历史中缺失 `reasoning_content` 的 assistant tool-call 消息，避免后续请求触发 `thinking is enabled but reasoning_content is missing`。
- 修复 core 同轮二次请求前的临时 `ToolCallsResult`，为 assistant tool-call 内容补入空 thinking 块，使 OpenAI payload 转换阶段能生成 `reasoning_content`，同时保留正常工具返回文本。
- 后台追答和主会话工具历史写入时保留主模型 thinking 内容与签名，确保后续轮次能正确回放工具调用上下文。

## 1.1.1 - 2026-03-19

- 移除主模型请求阶段对 `tool` / `tool_calls` 历史的清洗，避免已发生工具调用被下一轮对话灾难性遗忘。
- 为 `call_maid` 的 `dispatch / steer / stop / done` 全动作补齐主会话结构化历史记录，格式对齐 AstrBot 原版 `assistant(tool_calls)` + `tool(result)` 消息对。
- 在后台结果真正回到大小姐时，额外补写 `call_maid` 的结果记录，确保后续轮次能感知“大小姐确实调用过管家且已收到结果”。
- 统一 `call_maid` 工具记录的消息构造逻辑，集中处理参数、`tool_call_id` 与 thinking 兼容字段，避免多处手工拼装格式漂移。
- 修复 OpenAI / Responses 在 thinking 开启时对 assistant tool-call 消息缺失 `reasoning_content` 的 400 报错。

## 1.1.0 - 2026-03-18

- 将主模型调度入口从 XML 协议迁移为原生 `call_maid` Function Call，`dispatch` 改为“登记后后台执行”而非阻塞当前主链路。
- 新增 `hide_transfer_tools` 配置开关；当 `hide_native_tools=false` 时可独立隐藏 AstrBot 原生 `transfer_to_*` 工具。
- 修复后台回灌消息未写入主对话历史的问题，避免任务完成后下一轮对话仍误判为“管家仍在执行中”。
- 移除主模型额外提示词注入，改由 `call_maid` 工具自身的描述承担动作说明。

## 1.0.1 - 2026-03-17

- 新增 `hide_native_tools` 配置开关，可按需保留或隐藏大小姐可见的 AstrBot 原生工具。
- 支持单轮回复中解析多个 `<call_maid ...>` 标签，并以 batch 方式并发调度多个管家子任务。
- 为 batch 子任务补齐独立 session、统一汇总回灌、批量 `/maid status`、整批 `/maid stop` 与 batch steering 拒绝语义。
- 修复 `prompt_injector.py` 日志导入，统一改为 `from astrbot.api import logger`。
- 修复单任务在 `stopped / error` 终态下的 session 收尾，避免脏上下文污染下次复用。
- 为 `background_registry` 与 `batch_registry` 增加完成态清理，避免后台任务与 batch 记录长期堆积。
- 调整 batch 停止链路，改为按 batch runner 显式 `request_stop()`，避免共享 event 注册表导致的停止遗漏。
- 重新通过 `ruff format`、`ruff check --fix`、`py_compile`、`compileall` 与 IDE 诊断验证。

## 1.0.0 - 2026-03-15

- 初始化 `代理女仆` 插件，提供“大小姐 + 管家”双代理模式。
- 主模型默认禁用原生工具，仅通过 XML 协议块调用后台管家 subagent。
- 新增基于 `<call_maid ...>` 的协议解析与用户可见输出清洗。
- 支持单 active 管家 session 持久化、超时失效与跨轮复用。
- 新增后台任务注册表，支持查询运行状态、停止任务与对运行中的管家进行 steering。
- 新增 `/maid status`、`/maid stop` 控制入口，以及主模型可见的 `status / stop / steer` 控制标签。
- 子 agent 运行链路对接 AstrBot 的 active runner / stop 机制，并补齐上下文压缩与 context summary 所需的上下文预算参数。
- 增加子 agent 上下文预算、估算 token、实际 token usage 等调试日志。
- 新增“服侍模式”，可在用户未继续发言时由大小姐按协议主动追加多轮回复。
- 提供可配置的主模型协议提示模板、管家调度提示模板、服侍模式续发提示词与最大续发轮数。
