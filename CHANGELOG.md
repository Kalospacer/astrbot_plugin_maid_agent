# Changelog

## 1.0.0 - 2026-03-15
- 初始化 `代理女仆` 插件，提供“大小姐 + 管家”双代理模式。
- 主模型默认禁用原生工具，仅通过 XML 协议块调用后台管家 subagent。
- 新增 `<call_maid>`、`<maid_session>`、`<maid_control>` 协议解析与用户可见输出清洗。
- 支持单 active 管家 session 持久化、超时失效与跨轮复用。
- 新增后台任务注册表，支持查询运行状态、停止任务与对运行中的管家进行 steering。
- 新增 `/maid status`、`/maid stop` 控制入口，以及主模型可见的 `status / stop / steer` 控制标签。
- 子 agent 运行链路对接 AstrBot 的 active runner / stop 机制，并补齐上下文压缩与 context summary 所需的上下文预算参数。
- 增加子 agent 上下文预算、估算 token、实际 token usage 等调试日志。
- 新增“服侍模式”，可在用户未继续发言时由大小姐按协议主动追加多轮回复。
- 提供可配置的主模型协议提示模板、管家调度提示模板、服侍模式续发提示词与最大续发轮数。
