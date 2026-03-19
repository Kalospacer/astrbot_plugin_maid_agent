# Changelog

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
