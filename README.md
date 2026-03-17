<div align=\"center\">
  <h1>AstrBot Plugin Maid Agent</h1>
  <i>—— 代理女仆 ——</i>
</div>

<p align=\"center\">
  <strong>基于 AstrBot 的“大小姐 + 管家”双代理模式插件</strong>
</p>

---

本项目受到项目 `Muika-After-Story` 启发，用于验证一种旨在优化 LLM 角色扮演能力的概念架构——**“大小姐-管家”模式**。

在传统的角色扮演 Agent 架构中，大量 Function Calling 系统提示词的注入往往会导致模型出现“过拟合”问题，使得模型说话风格变得像刻板的 AI 助手，从而失去原有的角色扮演沉浸感。

本插件的解决方案：
1. **主模型（大小姐）**：过滤主对话模型的系统提示词，剔除过于结构化的函数调用 schema，使其上下文只有高度纯净的自然语言对话。负责和用户聊天、理解意图，并在需要时通过自然语言和 XML 标签向管家下达指令。
2. **子代理（管家）**：剥离并接管原本属于主模型的工具调用能力。管家主动启发式捕捉用户和大小姐的需求，在后台调用 SubAgent、工具、Shell 或浏览器等执行任务，最后将报告返回给大小姐。最终由大小姐与用户进行无感对话，保障完美的角色扮演体验。

> [!WARNING]
> **开发阶段警告**
> 
> - 本项目的实现不能完全代表该概念的最终形态，演进方向仍在积极探索中。
> - 由于 AstrBot 中的 `subagent` 模块仍处于实验性开发阶段，本插件需 Hook 并隐藏模型可用工具。此过滤提示词的设计目标，可能与其他插件的工作原理天然冲突，使用时或许会遇到一些问题。
> - 若与其他依赖“提示词注入”或“发送事件钩子 (Hook)”的插件同时使用，效果可能不达预期。
> - 如果你遇到问题，欢迎你提交issue。如果你有新的创意或愿意帮助修复bug，欢迎你提交pr。

## ✨ 快速开始
启用插件，确保你启用了AstrBot中的SubAgent 编排子代理功能，并且配置了至少一个可用的subagent。
在插件配置中，正确填写你的默认管家 Agent 名称为你刚配置的subagent ID。

## 🧩 核心机制

在主模型需要后台执行动作时，不会直接暴露原生工具，而是通过 **XML 标签** 表达意图。插件负责解析请求、调度管家并在后台回灌结果。

当前协议默认收敛为单标签形态：

- **调用管家**：\`<call_maid agent=\"...\">...</call_maid>\`
- **停止任务**：\`<call_maid action=\"stop\" />\`
- **补充要求**：\`<call_maid action=\"steer\">补充要求</call_maid>\`
- **结束任务**：\`<call_maid action=\"done\" />\`

**标准交互执行流：**
1. 用户发送消息。
2. 大小姐先输出第一轮自然语言回复。
3. 若需后台执行任务，大小姐在回复末尾附加 \`<call_maid>\` 标签。
4. 插件解析输出，拦截对外可见结果，并在后台调度目标 SubAgent（管家）。
5. 管家执行完毕，将结果回灌给大小姐。
6. 大小姐根据管家的结果，生成第二轮面向用户的自然语言回复。
7. 在对外显示时，所有内部运作的 XML 标签都会被自动清洗，完全隐形。

### 服侍模式

服侍模式用于打破传统的 `user -> assistant` 单轮回复节奏，让大小姐在**对方没有继续发言**时，也可以主动追加几轮自然语言回复。

- 通过命令 \`/maid_serve\` 切换当前会话的服侍模式开关
- 服侍模式会先检查插件配置里的全局开关 \`serving_mode_enabled\`；全局关闭时，任何会话都不会自动连发
- 即使全局开关已开启，当前会话也仍需手动执行一次 \`/maid_serve\` 才会真正启用
- 当前会话开启后，每次对方发言并收到大小姐首条回复后，插件都会自动再次请求 LLM，并按 \`serving_prompt_template\` 续发
- 单次对话触发的自动续发最多执行 \`serving_max_turns\` 次，默认上限为 `3`

## ✨ 当前能力

- [x] 主模型请求阶段清洗非自然语言上下文
- [x] 主模型原生工具强制禁用
- [x] XML 调度协议的双向注入与解析
- [x] 子 Agent (SubAgent) 的主动调度与结果回灌闭环
- [x] 面向对外显示的输出结果自动清洗
- [x] 单线并行活跃的管家 Session 状态持久化
- [x] Session 超时无感失效流转
- [x] Follow-up 第二轮回复深度清洗机制
- [x] 管家 Runner 完美透传 AstrBot 的上下文压缩配置
- [x] 服侍模式自动连发
- [x] 后台任务状态查询 / 停止 / steering

## 📦 Session 机制

插件目前采用 **“单 Active Session”** 模式设计：

- 每个通信来源 (\`unified_msg_origin\`) 同时只维护一个处于 Active 状态的管家 Session。
- 只要当前 Session 不被主动关闭，后续对管家的调度指令都会追加复用该 Session 的完整上下文。
- 当大小姐觉得任务完结并输出 \`<call_maid action=\"done\" />\` 后，当前 Session 会被正式关闭。
- 当 Session 超过设定的 \`session_timeout_minutes\` 阈值未被操作时，会自动作废重建。

> **数据存储**：
> Session 的通信数据不污染 AstrBot 的主 Conversation 存储，而是独立保存在插件的数据目录下：
> \`data/plugin_data/astrbot_plugin_maid_agent/\`

## ⚙️ 运行依赖

- 依赖 **AstrBot `>= 4.20.0`** （旧版本不保证兼容性）。
- **必须启用** AstrBot 系统的 **SubAgent (子代理编排)** 功能，并确保在配置中至少存在一个处于可用状态的 SubAgent。

## 🛠️ SubAgent 配置示例

下面提供一个对接所需的最小化 SubAgent 配置供参考（写在 AstrBot 全局配置或管理面板中）：

\`\`\`yaml
subagent_orchestrator:
  agents:
    - name: muiceagent
      enabled: true
      system_prompt: |
        你是运行在 AstrBot 中的 MuiceAgent，一个基于终端的编码助手，你目前作为子代理接收主代理的指令并实际执行。AstrBot 是一个开源的一站式 Agentic 个人和群聊助手。我们期望你做到精确、安全并且有帮助。
        # 你是沐雪的子 Agent

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
> - \`name\` 不一定要命名为 \`muiceagent\`，只要与你插件配置内的设定一致即可。
> - 插件对调用的 Agent Name 实现了大小写适配；若匹配不到目标代理，会自动回退至列表中第一个可用的 SubAgent。

## ⚙️ 插件配置

> 本插件的配置完全通过 **AstrBot 插件配置页** 进行管理，不再从全局的 \`maid_mode:\` 节点读取配置。

最小化默认配置参考：

\`\`\`yaml
default_agent_name: \"muiceagent\"
allowed_agent_names:
  - \"muiceagent\"
call_tag_name: \"call_maid\"
hide_native_tools: true
include_raw_user_input: true
session_enabled: true
serving_mode_enabled: false
serving_max_turns: 3
serving_prompt_template: "<maid_think>{maid_last_reply_block}根据我之前的回复，我应该继续说话</maid_think>"
session_timeout_minutes: 20
\`\`\`

### 配置项速查表

| 配置项 | 描述 |
|--------|------|
| \`default_agent_name\` | 默认被调度的 SubAgent 名称。 |
| \`allowed_agent_names\` | 允许的大小姐 XML 显式指定的 Agent 白名单列表。 |
| \`call_tag_name\` | 调度管家时主模型输出的 XML 标签名。 |
| \`hide_native_tools\` | 是否隐藏大小姐可见的 AstrBot 原生工具。关闭后，大小姐仍保留原生工具，同时也能继续使用管家协议。 |
| \`include_raw_user_input\` | 是否把真实的用户原话一并透传给管家。 |
| \`session_enabled\` | 是否启用管家的 Session 上下文持久化/状态留存机制。 |
| \`serving_mode_enabled\` | 服侍模式的全局总开关。开启后，会话仍需通过 \`/maid_serve\` 手动启用。 |
| \`serving_max_turns\` | 单次用户发言后，大小姐最多还能主动续说几次。 |
| \`serving_prompt_template\` | 服侍模式中系统自动再次请求 LLM 时使用的提示词模板，可用占位符：\`{maid_last_reply_block}\`。 |
| \`session_timeout_minutes\` | 并发 Session 闲置自动失效的分钟数。 |
| \`main_system_prompt_template\` | 注入给主模型（大小姐）的协议说明提示词模板。 |
| \`dispatch_prompt_template\` | 发送给管家执行机时的中继调度系统提示词模板。 |

## 📝 提示词模板

本系统内置了两层关键提示词模板，均支持通过配置进行重载。

### 1. 主模型协议提示模板 (大小姐侧)

配置项：\`main_system_prompt_template\`

支持的注入占位符：
- \`{call_tag_name}\`
- \`{default_agent_name}\`

**默认模板效果：**

\`\`\`text
- 需要管家协助时，回复末尾附加：<{call_tag_name} agent=\"{default_agent_name}\">任务要求</{call_tag_name}>
- 不需要管家则不附加此标签
- 停止管家任务：<{call_tag_name} action=\"stop\" />
- 补充或修正当前管家任务：<{call_tag_name} action=\"steer\">补充要求</{call_tag_name}>
- 管家任务结束时附加：<{call_tag_name} action=\"done\" />，未结束不附加
\`\`\`

### 2. 管家调度提示模板 (管家侧)

配置项：\`dispatch_prompt_template\`

支持的注入占位符（插件运行时自动装载）：
- \`{user_input_block}\`：真实的用户原话块。
- \`{maid_full_reply_block}\`：大小姐完整的自然语言回复上下文块。
- \`{maid_request_block}\`：从 \`<call_maid>\` 标签里提取出的显式任务需求文本块。

**默认模板效果：**

\`\`\`text
{user_input_block}{maid_full_reply_block}{maid_request_block}你是MuiceMaid，一个全能的管家AIagent助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。你需要综合考虑大小姐和对方的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断对方的需求，和大小姐的意图，如果大小姐误解了对方的需求，你以对方的需求为准完成任务，如果大小姐拒绝了对方的请求，你应当停止工作并汇报结束，如果大小姐和对方的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是对方。
\`\`\`

### 3. 服侍模式续发提示词

配置项：\`serving_prompt_template\`

支持的注入占位符：
- \`{maid_last_reply_block}\`：大小姐上一句刚刚发送出去的纯文本回复。

默认值：

\`\`\`text
<maid_think>{maid_last_reply_block}根据我之前的回复，我应该继续说话</maid_think>
\`\`\`

该提示词只在服侍模式自动续发时使用，不会影响正常的首轮回复或管家调度逻辑。

### 4. 命令入口

- \`/maid_serve\`：切换当前会话的服侍模式开关
- \`/maid status\`：查看当前后台管家任务状态
- \`/maid stop\`：请求停止当前后台管家任务

---

## 📄 许可证 & 作者

- **许可证**: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- **作者**: Kalo
