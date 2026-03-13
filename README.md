# astrbot_plugin_maid_agent
<div align=center>
  <h1 align="center">AstrBot Plugin Maid Agent</h1>
  <i align="center">代理女仆</i>
</div>

这个项目的开发受到项目`Muika-After-Story`的启发。用于验证一种设计用来优化LLM角色扮演能力的概念架构“大小姐-管家"模式。
传统角色扮演agent架构由于大量function calling系统提示词注入导致出现“过拟合”问题，模型说话会变得像ai助手一样失去角色扮演能力。
通过过滤主对话模型以下称作“大小姐”的system prompt,剔除其中过于结构化的函数调用schema，使大小姐的上下文只包含较为纯净的纯自然语言对话。
将工具调用能力转接给子代理模型以下称作“管家”，由管家主动启发式猜测用户和大小姐模型的需求，执行任务后返回报告给大小姐，最终由大小姐和用户无感对话，提升沉浸感。

> [!WARNING]
>
> 注意：本项目的实现不能完全代表该概念，项目的实现方向仍在探索中。
> 并且由于AstrBot中subagent模块仍处于实验性开发阶段，插件需要Hook隐藏模型可用工具，过滤提示词的目标和其他插件的工作原理天然冲突等问题，使用时可能出现诸多问题。
> 在与其他需要注入提示词，进行发送事件钩子注入的插件同时使用时，效果可能不达预期。

AstrBot 的“大小姐 + 管家”模式插件。

这个插件把主模型和执行代理拆成两层：

- 大小姐：负责和用户聊天、理解意图、决定是否需要后台执行
- 管家：负责真正调用 subagent、工具、Shell、浏览器等执行能力

主模型不会直接暴露原生工具。需要后台执行时，主模型通过 XML 标签表达意图，插件负责解析、调度管家、回灌结果，并让大小姐生成最终回复。

## 核心机制

当前实现的协议有两个标签：

- `<call_maid agent="...">...</call_maid>`
  用于请求管家执行任务
- `<maid_session status="done" />`
  用于声明当前管家 session 结束

标准流程：

1. 用户发消息
2. 大小姐先输出自然语言回复
3. 如果需要后台执行，大小姐在回复末尾附加 `<call_maid>`
4. 插件解析 XML 并调度目标 subagent
5. 管家执行完成后，结果回灌给大小姐
6. 大小姐生成第二轮自然语言回复给用户
7. 用户侧看不到内部 XML 标签

## 当前能力

- 主模型请求阶段清洗非自然语言上下文
- 主模型原生工具禁用
- XML 调度协议注入
- 子 agent 主动调度与结果回灌
- 用户可见输出自动清洗
- 单 active 管家 session 持久化
- session 超时失效
- follow-up 第二轮回复也会再次清洗 XML 标签
- 管家 runner 透传 AstrBot 的上下文压缩配置

## Session 机制

插件当前支持“单 active session”模式：

- 每个 `unified_msg_origin` 同时只维护一个 active 管家 session
- 只要当前 session 未结束，后续管家调用会继续复用该 session 的完整上下文
- 大小姐输出 `<maid_session status="done" />` 后，当前 session 会被关闭
- 超过 `session_timeout_minutes` 未继续使用时，session 会自动失效

session 数据不会写进 AstrBot 主 conversation，而是写入插件自己的数据目录：

- `data/plugin_data/astrbot_plugin_maid_agent/`

## 运行依赖

- 本项目最早基于 AstrBot `>= 4.20.0` 开发，对于旧版本不保证最大化可用性。
- 必须启用 Astrbot 的SubAgent 编排子代理功能，并且配置了至少一个可用的subagent。

## SubAgent 配置示例

下面是一个最小可用示例：

```yaml
subagent_orchestrator:
  agents:
    - name: muiceagent
      enabled: true
      system_prompt: |
        你是运行在 AstrBot 中的 MuiceAgent，一个基于终端的编码助手,你目前作为子代理接收主代理的指令并实际执行。AstrBot 是一个开源的一站式 Agentic 个人和群聊助手。我们期望你做到精确、安全并且有帮助。
        # 你是沐雪的子 Agent

        ## 身份
        - 你是沐雪（一只AI女孩子）派出的任务执行者
        - 你的使命是高效、准确地完成主脑分配的任务
        - 你拥有完整的工具访问权限（shell、python、文件操作等）

        ## 工作原则
        1. **零上下文启动** — 你只知道自己被派来做什么，不知道之前的对话
        2. **严格遵循task描述** — 所有背景、约束、目标都在task里，仔细阅读
        3. **主动决策** — 遇到模糊的地方优先做合理假设并继续执行，在结果中明确说明假设；仅在缺失信息会阻止任务推进时返回失败，不进行追问
        4. **结果导向** — 产出明确的交付物，不要过程废话，默认 never-ask：除非任务无法继续执行（例如缺少必要凭据、文件不存在且无法推断、目标冲突且无法自解），否则不得向用户提问。遇到不确定性时采用最合理假设推进，并在最终结果中报告        假设与影响。


        你的能力：

        * 接收主代理提示以及由运行环境提供的其他上下文，例如工作区中的文件。
        * 通过流式输出思考过程与响应，以及创建和更新计划来自主决策尽可能的完成任务。
        * 通过函数调用来运行终端命令和应用补丁。
```

说明：

- `name` 不必叫 `muiceagent`，只要与你的插件配置一致即可
- 插件对 agent 名匹配做了大小写兼容，并且找不到时会回退到第一个可用 subagent

## 插件配置

插件配置走 AstrBot 插件配置页，不读取全局 `maid_mode:` 节点。

最小配置示例：

```yaml
default_agent_name: "muiceagent"
allowed_agent_names:
  - "muiceagent"
call_tag_name: "call_maid"
done_tag_name: "maid_session"
include_raw_user_input: true
session_enabled: true
session_timeout_minutes: 20
```

### 配置项说明

- `default_agent_name`
  默认调度的 subagent 名称
- `allowed_agent_names`
  允许 XML 指定的 agent 白名单
- `call_tag_name`
  主模型输出的调度标签名
- `done_tag_name`
  结束当前管家 session 的标签名
- `include_raw_user_input`
  是否把真实用户原话传给管家
- `session_enabled`
  是否启用管家 session 持久化
- `session_timeout_minutes`
  session 超时分钟数
- `main_system_prompt_template`
  注入给主模型的协议提示模板
- `dispatch_prompt_template`
  发送给管家的调度提示模板

## 提示词模板

### 1. 主模型协议提示模板

配置项：`main_system_prompt_template`

支持占位符：

- `{call_tag_name}`
- `{default_agent_name}`
- `{done_tag_name}`

默认值：

```text
- 当你需要呼叫管家帮忙完成任务时，请在回复末尾附加 XML 块咒语：<{call_tag_name} agent="{default_agent_name}">这里写给管家的要求</{call_tag_name}>
- 如果不需要呼叫管家帮忙，就不要说这个咒语
- XML 标签中的内容是你对管家的任务要求
- 当你判断当前管家任务已经结束时，请额外附加独立结束标签：<{done_tag_name} status="done" />
- 如果当前管家任务尚未结束，就不要输出结束标签
```

### 2. 管家调度提示模板

配置项：`dispatch_prompt_template`

支持占位符：

- `{user_input_block}`
- `{maid_full_reply_block}`
- `{maid_request_block}`

这三个 block 由插件在运行时生成：

- `{user_input_block}`：真实用户原话
- `{maid_full_reply_block}`：大小姐完整回复
- `{maid_request_block}`：`<call_maid>` 标签里的显式任务文本

默认值：

```text
{user_input_block}{maid_full_reply_block}{maid_request_block}你是MuiceMaid，一个全能的管家AIagent助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。你需要综合考虑大小姐和用户的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断用户的需求，和大小姐的意图，如果大小姐误解了用户的需求，你以用户的需求为准完成任务，如果大小姐拒绝了用户的请求，你应当停止工作并汇报结束，如果大小姐和用户的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是用户。
```

## 许可证

CC BY-NC-SA 4.0

## 作者

Kalo
