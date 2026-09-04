<div align="center">
  <h1>AstrBot Plugin Maid Agent</h1>
  <i>—— 代理女仆 ——</i>
</div>

<p align="center">
  <strong>基于 AstrBot 的“大小姐 + 管家”双代理模式插件</strong>
</p>

---

传统角色扮演 Agent 会把大量 Function Calling 系统提示词注入主模型，导致模型说话像刻板的 AI 助手、丢失沉浸感。本插件把「聊天」和「做事」拆成两个角色：

- **大小姐（主模型）**：上下文只保留纯净的自然语言对话，负责陪用户聊天、理解意图。需要做事时，通过 `maid_agent` 向管家下达指令。
- **管家（子代理 SubAgent）**：接管全部工具调用能力（Shell、Python、文件、浏览器等），执行任务并把结果汇报给大小姐，由大小姐转告用户。管家的执行过程有独立记录，不会写进大小姐的对话历史。

> [!WARNING]
> **开发阶段提醒**
>
> - 本项目的实现不代表该概念的最终形态，仍在积极演进中。
> - AstrBot 的 `subagent` 模块仍处于实验性阶段，本插件需 Hook 并隐藏模型可用工具；此设计可能与其他依赖「提示词注入」或「发送事件钩子」的插件冲突，同用时效果可能不达预期。
> - 遇到问题欢迎提交 issue；有创意或愿意修 bug 欢迎提交 PR。

---

## 运行前提

- AstrBot `>= 4.20.0`。
- 一个 subagent 都没配置也没关系：插件启动时会自动创建一个默认管家（名称取 `default_agent_name`，默认 `butler`，可以使用全部工具），已有任何 subagent 配置时绝不会覆盖。
- 想用自己配置的管家，参考文末「SubAgent 配置示例」。

---

## 快速开始

1. 启用本插件（零配置可用，默认管家会自动创建）。
2. 正常聊天。大小姐需要做事时会自动调用 `maid_agent`，管家执行并回报。
3. 想换管家时，把 `allowed_agent_names` 改成你的 SubAgent 名称列表即可。

---

## 工具 API

以下工具由插件注册给大模型，通过 function calling 调用，**用户通常无需手动触发**。每个工具只会作用于当前聊天来源创建的任务，不能跨会话操作。

### maid_agent —— 发起任务

创建一个任务，或最多 5 个批量任务。必须显式指定管家名称；名称不在白名单、或没有对应的 subagent 时，回退到默认管家 `default_agent_name`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `prompt` | string | 必填 | 交给管家的任务要求（自包含，含背景/约束/目标）。 |
| `subagent_type` | string | 必填 | 目标管家名称，必须在插件白名单内。 |
| `run_in_background` | bool | 可选 | `true` 立即后台执行并返回句柄；默认 `false` 前台等待结果。 |
| `resume_agent_id` | string | 可选 | 续接已有的管家 agent，沿用其执行环境。 |
| `tasks` | array | 可选 | 批量任务，每项 `{prompt, subagent_type, run_in_background?}`，最多 5 项，独立 agent 并发执行。 |

### maid_send_message —— 补充要求

向正在运行的管家 agent 追加一条补充消息（对执行中的任务相当于 steering）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `agent_id` | string | 必填 | 目标 agent ID。 |
| `message` | string | 必填 | 补充要求文本。 |

### maid_list_agents —— 查看任务列表

列出当前聊天来源创建的所有 agent、任务和执行状态（运行中 / 上次结果 / 执行环境 / dispatch ID）。

### maid_task_output —— 读取结果

按任务 ID 读取结果；任务还在跑时可阻塞等待。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 必填 | 任务 ID。 |
| `block` | bool | 可选 | 任务运行中是否等待，默认 `true`。 |
| `timeout_ms` | int | 可选 | 阻塞超时毫秒，默认 `30000`，最大 `600000`。 |

### maid_task_stop —— 停止任务

按任务 ID 请求停止一个运行中的任务。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 必填 | 要停止的任务 ID。 |

---

## 用户命令

在聊天里直接输入，管理管家任务。

| 命令 | 说明 |
| --- | --- |
| `/maid status` | 查看当前会话运行中的管家任务。 |
| `/maid stop` | 请求停止当前会话的全部运行中任务。 |

---

## 核心机制

**执行环境（`dispatch_session_mode`）**

聊天里新派的任务有两种执行环境，在任务开始时固定，中途不会切换：

- `foreground`（前台）：管家直接使用触发这次任务的**真实聊天消息**。用户发的图片、文件等原样可用，管家调用的第三方工具也作用在真实会话上。每个聊天来源同时最多一个前台任务；批量任务按顺序分配，分不到的自动改后台。
- `background`（后台，默认）：管家在隔离沙箱里执行，用户侧无感知，完成后只把一次结构化摘要回灌给大小姐。

控制台页面没有真实聊天消息，所以从控制台创建的任务**永远是**隔离沙箱（界面里会标注），不受这个配置影响。

**任务与并发**

- 每次新任务创建新 agent；只有显式 `resume_agent_id` 才续接。
- 每个聊天来源最多 5 个活跃任务，全局最多 20 个，超限整单拒绝（不会创建一半）。
- 前台任务如果超过 AstrBot 主模型工具超时还没跑完，会降级为后台句柄返回，管家继续执行、完成后主动通知大小姐。

**通知与续读**

后台任务完成后，插件会唤醒大小姐把结果摘要转告用户（有防重复机制）；没有等到通知时，大小姐也可以随时用 `maid_task_output` 主动读取。

**数据存储**：`data/plugin_data/astrbot_plugin_maid_agent/sessions/<session_id>/`，会话事件按序存 `events.jsonl`；图片附件存 `attachments/`。超过 `retention_days` 天没有活动的记录自动清理（管家记忆目录不受影响）。

---

## 控制台

插件页提供一个聊天式控制台（AstrBot 面板 → 插件 → 代理女仆 → 控制台）：可以直接给管家派任务、看流式回复与工具调用卡片、查看每步 token 消耗；左侧管理历史会话（搜索、重命名、删除、Fork），会话上会标注执行环境（独立沙箱 / 后台 / 前台）与投递状态；右上角设置弹窗可改插件配置与主题。

> 控制台任务全部运行在隔离沙箱。想测试需要真实聊天消息能力（如读群图片）的场景，请在聊天里由大小姐派发。

---

## 配置参考

配置全部在 AstrBot 插件配置页管理。**保存严格**：类型、范围、名单或模板错误会被整单拒绝并提示具体字段，不做静默修复；**加载容错**：旧版本遗留的键会被忽略、无效值回退默认，不会导致插件无法启动，首次成功保存后配置自动规范化。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `allowed_agent_names` | `[butler]` | 可调度的 SubAgent 白名单，不能为空。 |
| `default_agent_name` | `butler` | 名称不匹配时的回退管家；没有任何 subagent 时自动以该名创建一个。 |
| `hide_native_tools` | `true` | 隐藏主模型可见的 AstrBot 原生工具，只保留五个 maid 工具（改配置即时生效、无需重启）。 |
| `hide_transfer_tools` | `true` | `hide_native_tools=false` 时，额外隐藏主模型可见的全部 `transfer_to_*` 工具，maid 工具仍可用。 |
| `dispatch_session_mode` | `background` | 聊天任务执行环境：真实聊天消息（`foreground`）或隔离沙箱（`background`）。 |
| `include_raw_user_input` | `true` | 开启时把真实用户原话一并透传给管家；关闭后只传大小姐的任务请求。 |
| `log_raw_llm_io` | `false` | 开启后在 DEBUG 日志打印完整 LLM 原始请求/响应，可能包含敏感信息。 |
| `dispatch_prompt_template` | 内置 | 发送给管家的调度提示模板，只允许 `{user_input_block}`、`{maid_request_block}` 两个占位符（用户原话块 / 大小姐请求块）。管家的「身份」请配置在 SubAgent 的人格里，这里只管派活格式。 |
| `memory_agent_names` | `[]` | 启用持久记忆与原生 Read/Write/Edit 文件的管家名称列表。 |
| `max_active_per_umo` | `5` | 每个聊天来源的活跃任务上限。 |
| `max_active_global` | `20` | 全局活跃任务上限。 |
| `retention_days` | `30` | 会话记录保留天数（不删 memory）。 |
| `max_turn_seconds` | `1800` | 单轮看门狗超时（秒），超时强制终止并标记 `interrupted`；`0` 关闭看门狗。 |

---

## SubAgent 配置示例

在 AstrBot 全局配置或管理面板中配置一个 SubAgent（`name` 需在插件 `allowed_agent_names` 白名单内，插件对名称做大小写适配）：

```yaml
subagent_orchestrator:
  agents:
    - name: butler
      enabled: true
      system_prompt: |
        你是运行在 AstrBot 中的管家，一个基于终端的执行助手，
        作为子代理接收主代理的指令并实际执行。

        ## 工作原则
        1. 零上下文启动 —— 你只知道自己被派来做什么。
        2. 严格遵循 task 描述 —— 背景、约束、目标都在 task 里。
        3. 主动决策 —— 遇到模糊处优先做合理假设并继续，在结果中说明假设；
           仅在任务完全阻断时才返回失败，不进行无休止追问。
        4. 结果导向 —— 产出明确交付物，不要过程废话。
```

> 不想手动配置？什么都不填即可——插件会自动创建一个名为 `butler` 的默认管家（使用全部工具，人格与调度模板同源）。

---

## 许可证 & 作者

- **许可证**：[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- **作者**：Kalo
