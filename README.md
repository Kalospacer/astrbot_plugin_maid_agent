<div align=\"center\">
  <h1>AstrBot Plugin Maid Agent</h1>
  <i>—— 代理女仆 ——</i>
</div>

<p align=\"center\">
  <strong>基于 AstrBot 的“大小姐 + 管家”双代理模式插件</strong>
</p>

---

## 1.3.0 — Claude Code 风格 Subagent Runtime

1.3.0 将原有的“回复发送后统一后台执行”重构为 **foreground-first** runtime，对齐 Claude Code 的 AgentTool 语义：

- **前台同步优先**：`call_maid` 默认在前台同步等待管家执行（最多 50 秒），短任务在同一 tool turn 直接返回结果给主模型；超时后同一 runner 原地转后台继续执行，返回带 `background_reason=timeout` 的结构化句柄。
- **稳定 agent_id + 独立 task_id**：新 dispatch 永远创建新 agent；显式 `resume_agent_id` 才恢复。running agent 的 resume 作为 steer 注入下一 tool round，terminal/interrupted 的 resume 创建新 task 始终后台执行。
- **`maid_task` 工具**：对齐 Claude TaskOutput，支持 `status/result/stop/steer`，`result` 默认阻塞 30 秒（最大 600 秒），成功读终态时认领 notification 避免重复唤醒。
- **批量并发**：`call_maid(tasks=[...])` 最多 5 项，原子预留容量，不足整批拒绝。
- **notification outbox**：终态生成稳定 `notification_id`，首次完成立即唤醒，无定时重试，仅在重启/新消息/`maid_task(result)` 时重试。snapshot 语义 + best-effort 去重。
- **隔离持久化**：`agents/<agent_id>/{agent.json,transcript.jsonl,runs/<task_id>.json,outputs/<task_id>.txt}`，与旧 `sessions/*.json` 完全隔离。transcript append-only，resume 过滤损坏尾部与未配对 tool calls。30 天清理（不删 memory）。
- **递归禁止 + memory**：child 移除 `call_maid`/`maid_task`/`transfer_to_*`；`memory_agent_names` opt-in 的 agent 自动获得原生 Read/Write/Edit，memory 以 UMO+agent_name 隔离。
- **并发容量**：每会话最多 5 个 active runs，全局最多 20，超限立即拒绝。

旧 `call_maid(action=...)` 接口保留兼容转换并输出弃用提示。不修改 AstrBot Core 任何文件。

---

本项目受到项目 `Muika-After-Story` 启发，用于验证一种旨在优化 LLM 角色扮演能力的概念架构——**“大小姐-管家”模式**。

在传统的角色扮演 Agent 架构中，大量 Function Calling 系统提示词的注入往往会导致模型出现“过拟合”问题，使得模型说话风格变得像刻板的 AI 助手，从而失去原有的角色扮演沉浸感。

本插件的解决方案：

1. **主模型（大小姐）**：过滤主对话模型的系统提示词，剔除过于结构化的函数调用 schema，使其上下文只有高度纯净的自然语言对话。负责和用户聊天、理解意图，并在需要时通过原生 `call_maid` Function Call 向管家下达指令。
2. **子代理（管家）**：剥离并接管原本属于主模型的工具调用能力。管家主动启发式捕捉用户和大小姐的需求，在后台调用 SubAgent、工具、Shell 或浏览器等执行任务，最后将报告返回给大小姐。最终由大小姐与用户进行无感对话，保障完美的角色扮演体验。

> [!WARNING] > **开发阶段警告**
>
> - 本项目的实现不能完全代表该概念的最终形态，演进方向仍在积极探索中。
> - 由于 AstrBot 中的 `subagent` 模块仍处于实验性开发阶段，本插件需 Hook 并隐藏模型可用工具。此过滤提示词的设计目标，可能与其他插件的工作原理天然冲突，使用时或许会遇到一些问题。
> - 若与其他依赖“提示词注入”或“发送事件钩子 (Hook)”的插件同时使用，效果可能不达预期。
> - 如果你遇到问题，欢迎你提交 issue。如果你有新的创意或愿意帮助修复 bug，欢迎你提交 pr。

## ✨ 快速开始

启用插件，确保你启用了 AstrBot 中的 SubAgent 编排子代理功能，并且配置了至少一个可用的 subagent。
在插件配置中，正确填写你的默认管家 Agent 名称为你刚配置的 subagent ID。

## 🧩 核心机制

在主模型需要后台执行动作时，插件通过原生 **`call_maid` Function Call** 表达意图。`dispatch` 只负责登记后台任务并立即返回，真正的子 Agent 执行仍在后台完成，结束后再回灌给大小姐。

当前工具动作包括：

- **发起任务**：`call_maid(action=\"dispatch\", agent_name=\"...\", request_text=\"...\")`
- **批量发起任务**：同一轮多次调用 `dispatch`，插件会将它们视为同一个 batch 并发执行
- **停止任务**：`call_maid(action=\"stop\")`
- **补充要求**：`call_maid(action=\"steer\", request_text=\"补充要求\")`
- **结束任务**：`call_maid(action=\"done\")`

**标准交互执行流：**

1. 用户发送消息。
2. 若需执行任务，大小姐直接调用 `call_maid`，并传入自包含的 `request_text`。
3. 短任务在当前 tool turn 内返回；超过 foreground 阈值后，同一 runner 原地转后台。
4. 后台 run 结束后写入持久化 notification outbox，并唤醒大小姐整理结果。
5. 可用 `maid_task(status/result/stop/steer)` 按 task/agent ID 查询或控制。

## ✨ 当前能力

- [x] 主模型请求阶段清洗非自然语言上下文
- [x] `hide_native_tools` 可配置控制大小姐是否暴露 AstrBot 原生工具
- [x] 原生 `call_maid` 工具调度
- [x] `call_maid(tasks=[...])` foreground/background 混合 batch 并发调度
- [x] 子 Agent (SubAgent) 的主动调度与结果回灌闭环
- [x] batch 结果统一汇总后仅回灌一次给大小姐
- [x] 面向对外显示的输出结果自动清洗
- [x] 稳定 `agent_id` + 每次运行独立 `task_id`
- [x] 每 UMO 5 / 全局 20 的 active run 原子容量控制
- [x] Follow-up 第二轮回复深度清洗机制
- [x] 管家 Runner 完美透传 AstrBot 的上下文压缩配置
- [x] 后台任务状态查询 / 停止 / steering
- [x] `agent_id` / `parent_message_id` 父子溯源与精确历史回灌
- [x] chat / batch / dashboard 统一任务分流与原子活跃任务占用
- [x] append-only JSONL transcript 与不完整 tool-call 轨迹过滤
- [x] 重启遗留 run 静默收敛为 `interrupted`

## 📦 Agent / Run Runtime

- 新 dispatch 永远创建新 agent；只有显式 `resume_agent_id` 才恢复稳定身份。
- 每个 agent 同时最多一个 active run；每个 UMO 可并发多个不同 agent。
- running agent 的 resume 转为 steer；terminal/interrupted agent 的 resume 创建新 task 并后台执行。
- `action=done` 在 1.3.x 仅为无状态兼容 no-op，1.4 将移除。

> **数据存储**：`data/plugin_data/astrbot_plugin_maid_agent/agents/<agent_id>/`。
> 旧 `sessions/*.json` 保留但不作为 1.3 runtime 状态真源。

## ⚙️ 运行依赖

- 依赖 **AstrBot `>= 4.20.0`** （旧版本不保证兼容性）。
- **必须启用** AstrBot 系统的 **SubAgent (子代理编排)** 功能，并确保在配置中至少存在一个处于可用状态的 SubAgent。

## 🛠️ SubAgent 配置示例

下面提供一个对接所需的最小化 SubAgent 配置供参考（写在 AstrBot 全局配置或管理面板中）：

\`\`\`yaml
subagent_orchestrator:
agents: - name: muiceagent
enabled: true
system_prompt: |
你是运行在 AstrBot 中的 MuiceAgent，一个基于终端的编码助手，你目前作为子代理接收主代理的指令并实际执行。AstrBot 是一个开源的一站式 Agentic 个人和群聊助手。我们期望你做到精确、安全并且有帮助。 # 你是沐雪的子 Agent

        ## 身份
        - 你是沐雪（一只AI女孩子）派出的任务执行者
        - 你的使命是高效、准确地完成主脑分配的任务
        - 你拥有完整的工具访问权限（shell、python、文件操作等）

        ## 工作原则
        1. **零上下文启动** — 你只知道自己被派来做什么，不知道之前的对话。
        2. **严格遵循 task 描述** — 所有背景、约束、目标都在 task 里，仔细阅读。
        3. **主动决策** — 遇到模糊的地方优先做合理假设并继续执行，在结果中明确说明假设；仅在缺失信息会导致任务完全阻断时返回失败，不进行无休止追问。
        4. **结果导向** — 产出明确的交付物，不要过程废话，默认 never-ask：除非任务无法继续执行（例如缺少必要凭证、文件不存在且无法推断、目标冲突且无法自解），否则不得向用户提问。遇到不确定性时采用最合理假设推进，并在最终结果中报告假设与影响。

        你的能力：
        * 接收主代理提示以及由运行环境提供的其他上下文（如工作区中的文件等）。
        * 通过流式输出思考过程与响应，并通过创建和更新计划来自主决策，尽最大可能完成任务。
        * 通过函数调用来运行终端命令、修改文件。

\`\`\`

> **注意**：
>
> - \`name\` 不一定要命名为 \`muiceagent\`，只要与你插件配置内的设定一致即可。
> - 插件对调用的 Agent Name 实现了大小写适配；若匹配不到目标代理，会自动回退至列表中第一个可用的 SubAgent。

## ⚙️ 插件配置

> 本插件的配置完全通过 **AstrBot 插件配置页** 进行管理，不再从全局的 \`maid_mode:\` 节点读取配置。

最小化默认配置参考：

```yaml
default_agent_name: "muiceagent"
allowed_agent_names:
  - "muiceagent"
hide_native_tools: true
hide_transfer_tools: true
include_raw_user_input: true
foreground_timeout_seconds: 50
memory_agent_names: []
max_active_per_umo: 5
max_active_global: 20
retention_days: 30
```

### 配置项速查表

| 配置项                          | 描述                                                                                            |
| ------------------------------- | ----------------------------------------------------------------------------------------------- |
| \`default_agent_name\`          | 默认被调度的 SubAgent 名称。                                                                    |
| \`allowed_agent_names\`         | 允许 \`call_maid(dispatch)\` 显式指定的 Agent 白名单列表。                                      |
| \`hide_native_tools\`           | 是否隐藏主模型可见的 AstrBot 原生工具。开启后只保留 `call_maid` 与 `maid_task`。                 |
| \`hide_transfer_tools\`         | 当 \`hide_native_tools=false\` 时，是否额外隐藏全部 \`transfer_to_*\` 工具。                  |
| \`include_raw_user_input\`      | 是否把真实的用户原话一并透传给管家。                                                            |
| `foreground_timeout_seconds`    | 前台等待阈值，默认 50 秒；超时后同一 runner 转后台。                                            |
| `memory_agent_names`            | 启用持久记忆和原生 Read/Write/Edit 的 agent 名称列表。                                          |
| `max_active_per_umo`            | 每 UMO foreground/background 活跃 run 总上限。                                                   |
| `max_active_global`             | 全局 foreground/background 活跃 run 总上限。                                                     |
| `retention_days`                | agent 元数据、run、transcript、output 的保留天数；不删除 memory/旧 sessions。                    |
| \`dispatch_prompt_template\`    | 发送给管家执行机时的中继调度系统提示词模板。                                                    |

## 📝 提示词模板

本系统内置了一层关键调度提示词模板，支持通过配置进行重载。

### 1. 管家调度提示模板 (管家侧)

配置项：\`dispatch_prompt_template\`

支持的注入占位符（插件运行时自动装载）：

- \`{user_input_block}\`：真实的用户原话块。
- \`{maid_request_block}\`：从 \`call_maid\` 中提取出的自包含任务需求文本块。

1.2 配置中的 \`{maid_full_reply_block}\` 在 1.3.x 仍可读取，但 foreground-first runtime 会将其映射为空并记录弃用警告；1.4 将移除该兼容占位符。真正未知或格式损坏的占位符会回退到默认模板，避免任务直接失败。

**默认模板效果：**

\`\`\`text
{user_input_block}{maid_request_block}你是 MuiceMaid，一个全能的管家 AIagent 助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。你需要综合考虑大小姐和对方的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断对方的需求，和大小姐的意图，如果大小姐误解了对方的需求，你以对方的需求为准完成任务，如果大小姐拒绝了对方的请求，你应当停止工作并汇报结束，如果大小姐和对方的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是对方。
\`\`\`

### 2. 命令入口

- \`/maid status\`：查看当前后台管家任务状态；若当前为 batch，会展示子任务明细
- \`/maid stop\`：请求停止当前后台管家任务；若当前为 batch，会停止整批任务

---

## 📄 许可证 & 作者

- **许可证**: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- **作者**: Kalo
