# AstrBot 大小姐管家模式插件

## 概述

本插件实现“大小姐 + 管家”模式，将 AstrBot 的主对话模型与执行代理进行角色分离：

- **大小姐（主模型）**：仅保留自然语言对话上下文，不直接持有任何原生工具，专注于对话和理解用户意图
- **管家（SubAgent）**：负责所有工具调用、Bash 执行等幕后操作，接收插件拼装的双通道输入（用户原始输入 + 大小姐的自然语言要求）

当前版本已切换为 **XML 调度协议**：

- 大小姐需要幕后执行时，不再调用 `transfer_to_*` 原生 handoff 工具
- 而是在回复中输出 `<call_maid>...</call_maid>` XML 块表达请求
- 插件负责解析 XML、调度对应子 agent，并将结果回灌给大小姐
- 用户最终只看到大小姐的自然语言回复，不会看到 XML 标签残留

## 功能特性

1. **主模型上下文净化**：自动过滤 tool role 消息和 tool_calls 痕迹
2. **原生工具隔离**：主模型请求阶段显式禁用 `req.func_tool`，不再暴露原生 handoff / tool call 能力
3. **XML 调度协议**：主模型通过 `<call_maid>` 标签表达幕后执行意图
4. **子 agent 闭环调度**：插件主动调用目标子 agent，并将执行结果回灌给大小姐
5. **用户可见输出清洗**：最终发送给用户前会清理 `<call_maid>` 标签与残留片段

## 安装

将本插件目录放置在 AstrBot 的 `data/plugins/` 目录下，或通过插件市场安装。

```bash
# 手动安装
cp -r astrbot_plugin_maid_agent /path/to/astrbot/data/plugins/
```

## 配置

### 1. 启用插件

在 AstrBot WebUI 的插件管理页面启用“大小姐管家模式”插件。

### 2. 配置管家 SubAgent

在 AstrBot 主配置文件中添加以下内容：

```yaml
subagent_orchestrator:
  agents:
    - name: butler
      enabled: true
      system_prompt: |
        你是管家，负责处理大小姐转交的所有执行任务。

        你会同时收到：
        1. 用户的原始输入
        2. 大小姐的自然语言要求

        请综合考虑以上信息，执行必要的工具调用来完成任务。
        执行完成后，用自然语言向大小姐汇报结果。

        注意：你的汇报对象是大小姐，而不是直接面向用户。
      tools: null
```

### 3. 配置插件协议参数（可选）

本插件使用 AstrBot 插件配置注入，不读取全局主配置里的 `maid_mode:` 节点。

请在插件管理页面打开本插件配置，填写以下字段：

```yaml
default_agent_name: "butler"
allowed_agent_names:
  - "butler"
call_tag_name: "call_maid"
include_raw_user_input: true
```

字段说明：

- `default_agent_name`：默认调度的子 agent 名称
- `allowed_agent_names`：允许 XML 指定的 agent 白名单
- `call_tag_name`：XML 标签名，默认 `call_maid`
- `include_raw_user_input`：是否把用户原始输入一起传给子 agent

### 4. （可选）为管家指定专属模型

如果希望管家使用不同的模型（如更强大的模型），可以指定 `provider_id`：

```yaml
subagent_orchestrator:
  agents:
    - name: butler
      enabled: true
      provider_id: "openai_gpt4"
      system_prompt: |
        ...
      tools: null
```

## 工作流程

```text
用户消息
  ↓
大小姐模型（自然语言对话）
  ↓
需要幕后执行？
  ↓ 是
输出 <call_maid agent="butler">...</call_maid>
  ↓
插件解析 XML
  ↓
调度目标 SubAgent（双通道输入）
  ↓
执行工具 / Bash / 其他能力
  ↓
自然语言结果回灌给大小姐
  ↓
大小姐重新组织回复
  ↓
用户
```

## 依赖要求

| 依赖项                | 说明                                                                               |
| --------------------- | ---------------------------------------------------------------------------------- |
| AstrBot >= 4.16       | 框架版本要求                                                                       |
| SubAgent Orchestrator | 需要启用并配置至少一个可用子 agent                                                 |
| 默认 agent            | 默认依赖名为 `butler` 的子 agent，除非在插件配置 `default_agent_name` 中另行指定   |

## 注意事项

1. **必须配置可用子 agent**：否则插件无法完成 XML 请求后的幕后执行
2. **当前 subagent 为一次性执行模型**：并不是持续共享上下文的长期管家会话
3. **当前版本不处理“大小姐中途继续和同一管家追问并保持同一工作记忆”的场景**
4. **最终用户输出会清洗 `<call_maid>` 标签**：用户不应看到内部协议痕迹
5. **旧的 `transfer_to_butler` 说明仅为兼容背景，不再是主路径协议**

## 当前实现说明

当前插件已经实现：

- 主模型请求阶段禁用原生工具暴露
- XML 标签解析
- 通过插件主动调度子 agent
- 子 agent 结果回灌给大小姐后再次生成用户回复
- 用户侧 `<call_maid>` 标签清洗

当前仍需注意：

- `response_validator.py` 仅保留为低优先级观测模块
- 当前 AstrBot 的子 agent 执行仍是一次性实例，不具备天然持续会话记忆

## 开发

```bash
# 安装开发依赖
pip install ruff

# 代码检查
ruff check .

# 代码格式化
ruff format .

# 语法检查
python -m compileall .
```

## 许可证

CC BY-NC-SA 4.0 (Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International)

本作品采用知识共享署名-非商业性使用-相同方式共享 4.0 国际许可协议进行许可。

**你可以自由地：**

- 共享 — 以任何媒介或格式复制、发行本作品
- 演绎 — 修改、转换或以本作品为基础进行创作

**惟须遵守下列条件：**

- 署名 — 你必须给出适当的署名
- 非商业性使用 — 你不得将本作品用于商业目的
- 相同方式共享 — 修改后的作品必须使用相同许可证

完整许可协议文本：https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.zh-Hans

## 作者

Kalo
