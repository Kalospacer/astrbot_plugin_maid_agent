<div align="center">
  <h1>AstrBot Plugin Maid Agent</h1>
  <i>—— 代理女仆 ——</i>
</div>

<p align="center">
  <strong>基于 AstrBot 的“大小姐 + 管家”双代理模式插件</strong>
</p>

---

传统角色扮演 Agent 会把大量 Function Calling 系统提示词注入主模型，导致模型说话像刻板的 AI 助手、丢失沉浸感。本插件把「聊天」和「做事」拆成两个角色：

- **大小姐（主模型）**：上下文只保留纯净的自然语言对话，负责陪用户聊天、理解意图。需要做事时，通过 `call_maid` 向管家下达指令。
- **管家（子代理 SubAgent）**：接管全部工具调用能力（Shell、Python、文件、浏览器等），在后台执行任务并把结果汇报给大小姐，由大小姐转告用户。

> [!WARNING]
> **开发阶段提醒**
>
> - 本项目的实现不代表该概念的最终形态，仍在积极演进中。
> - AstrBot 的 `subagent` 模块仍处于实验性阶段，本插件需 Hook 并隐藏模型可用工具；此设计可能与其他依赖「提示词注入」或「发送事件钩子」的插件冲突，同用时效果可能不达预期。
> - 遇到问题欢迎提交 issue；有创意或愿意修 bug 欢迎提交 PR。

---

## 运行前提

- AstrBot `>= 4.20.0`。
- 已在 AstrBot 中启用 **SubAgent（子代理编排）** 功能，并至少配置一个可用的 SubAgent。
- 在插件配置中，把 `default_agent_name` 填成你那个 SubAgent 的名称。

---

## 快速开始

1. 在 AstrBot 配置一个 SubAgent（可参考文末「SubAgent 配置示例」）。
2. 启用本插件，在插件配置页填好默认管家名称。
3. 正常聊天。大小姐需要做事时会自动调用 `call_maid`，管家在后台执行并回报。

---

## 工具 API

以下两个工具由插件注册给大模型，通过 function calling 调用，**用户通常无需手动触发**。

### call_maid —— 发起任务

调度管家执行任务。默认前台同步等待（最多 `foreground_timeout_seconds` 秒），短任务当场返回结果；超时自动转后台，返回任务句柄。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `request_text` | string | 发起时必填 | 交给管家的任务要求（自包含，含背景/约束/目标）。 |
| `agent_name` | string | 可选 | 目标管家名称，留空用默认管家。仅新建 agent 时生效。 |
| `resume_agent_id` | string | 可选 | 恢复已有 agent。running 时等价于 steer；终态时新建 task 后台执行。 |
| `run_in_background` | bool | 可选 | `true` 立即转后台并返回句柄；默认 `false` 前台等待。 |
| `tasks` | array | 可选 | 批量任务，每项 `{request_text, agent_name?, run_in_background?}`，最多 5 项。仅新建 agent。 |

### maid_task —— 查询与控制

对后台任务进行状态查询、读取结果、停止或补充要求，对齐 Claude TaskOutput 语义。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | string | 必填 | `status`（非阻塞查状态）/ `result`（阻塞等终态）/ `stop`（停止）/ `steer`（补充要求）。 |
| `task_id` | string | 按动作 | `status` / `result` / `stop` 时填写。 |
| `agent_id` | string | 按动作 | `steer` 时必填；`result` 可选用于归属校验。 |
| `message` | string | steer 必填 | `steer` 时补充的要求文本。 |
| `block` | bool | 可选 | `result` 是否阻塞等待，默认 `true`。 |
| `timeout_ms` | int | 可选 | `result` 阻塞超时毫秒，默认 `30000`，最大 `600000`。 |

---

## 用户命令

在聊天里直接输入，管理后台管家任务。

| 命令 | 说明 |
| --- | --- |
| `/maid status` | 查看当前后台管家任务状态；批量任务会展示子任务明细。 |
| `/maid stop` | 请求停止当前后台管家任务；批量任务会停止整批。 |

---

## 核心机制

**执行流程**

1. 用户发消息，大小姐判断需要做事时调用 `call_maid`。
2. 短任务在当前 tool turn 内直接返回结果；超过前台阈值后同一执行器原地转后台。
3. 后台任务结束后唤醒大小姐，由她整理结果转告用户。
4. 期间可用 `maid_task(status/result/stop/steer)` 按 task/agent ID 查询或控制。

**Agent 与 Run 模型**

- 每次新 dispatch 创建新 agent；只有显式 `resume_agent_id` 才恢复稳定身份。
- 每个 agent 同时最多一个活跃 run；每个会话可并发多个不同 agent。
- 每个会话最多 5 个活跃 run，全局最多 20，超限立即拒绝。

**数据存储**：`data/plugin_data/astrbot_plugin_maid_agent/sessions/<session_id>/`，含 `header.json`、`meta.json`、`events.jsonl`（按顺序记录会话内每个事件的日志）；图片附件在 `attachments/<session_id>/`。元数据按 `retention_days` 清理（不删 memory）。

---

## 控制台

插件页提供一个聊天式控制台（AstrBot 面板 → 插件 → 代理女仆 → 控制台）：可以新建会话直接给管家派任务、看流式回复与工具调用卡片、查看每步 token 消耗，左侧管理历史会话（搜索、重命名、删除、Fork），右上角设置弹窗可改插件配置与主题。

---

## 配置参考

配置全部在 AstrBot 插件配置页管理。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `default_agent_name` | `muiceagent` | 默认被调度的 SubAgent 名称。 |
| `allowed_agent_names` | `[muiceagent]` | `call_maid` 可显式指定的 Agent 白名单。 |
| `hide_native_tools` | `true` | 隐藏主模型可见的 AstrBot 原生工具，只保留 `call_maid` 与 `maid_task`。 |
| `hide_transfer_tools` | `true` | `hide_native_tools=false` 时，额外隐藏全部 `transfer_to_*` 工具。 |
| `include_raw_user_input` | `true` | 把真实用户原话一并透传给管家。 |
| `foreground_timeout_seconds` | `50` | 前台等待阈值（秒），超时转后台。 |
| `memory_agent_names` | `[]` | 启用持久记忆与原生 Read/Write/Edit 的 agent 名称列表。 |
| `max_active_per_umo` | `5` | 每会话活跃 run 上限。 |
| `max_active_global` | `20` | 全局活跃 run 上限。 |
| `retention_days` | `30` | agent 元数据/run/transcript/output 保留天数（不删 memory）。 |
| `dispatch_prompt_template` | 内置 | 发送给管家的中继调度系统提示词模板。 |

**调度提示词模板**（`dispatch_prompt_template`）支持两个占位符，运行时自动注入：

- `{user_input_block}`：真实用户原话块。
- `{maid_request_block}`：从 `call_maid` 提取的自包含任务需求块。

---

## SubAgent 配置示例

在 AstrBot 全局配置或管理面板中配置一个 SubAgent（`name` 与插件配置一致即可，插件对名称做大小写适配，匹配不到时回退到第一个可用 SubAgent）：

```yaml
subagent_orchestrator:
  agents:
    - name: muiceagent
      enabled: true
      system_prompt: |
        你是运行在 AstrBot 中的 MuiceAgent，一个基于终端的编码助手，
        目前作为子代理接收主代理的指令并实际执行。

        ## 工作原则
        1. 零上下文启动 —— 你只知道自己被派来做什么。
        2. 严格遵循 task 描述 —— 背景、约束、目标都在 task 里。
        3. 主动决策 —— 遇到模糊处优先做合理假设并继续，在结果中说明假设；
           仅在任务完全阻断时才返回失败，不进行无休止追问。
        4. 结果导向 —— 产出明确交付物，不要过程废话。
```

---

## 许可证 & 作者

- **许可证**：[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- **作者**：Kalo
