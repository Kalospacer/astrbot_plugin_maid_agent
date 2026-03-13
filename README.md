# AstrBot 大小姐管家模式插件

## 概述

本插件实现"大小姐 + 管家"模式，将 AstrBot 的主对话模型与执行代理进行角色分离：

- **大小姐（主模型）**：仅保留自然语言对话上下文，不直接持有业务工具，专注于对话和理解用户意图
- **管家（Butler SubAgent）**：负责所有工具调用、Bash 执行等操作，接收双通道输入（用户原始输入 + 大小姐的自然语言要求）

## 功能特性

1. **主模型上下文净化**：自动过滤掉 tool role 消息和 tool_calls 痕迹
2. **工具列表裁剪**：主模型只看到 `transfer_to_butler` handoff 工具
3. **双通道输入注入**：管家同时获取原始用户输入和大小姐的转述
4. **模式说明注入**：自动为主模型注入大小姐角色说明

## 安装

将本插件目录放置在 AstrBot 的 `data/plugins/` 目录下，或通过插件市场安装。

```bash
# 手动安装
cp -r astrbot_plugin_maid_agent /path/to/astrbot/data/plugins/
```

## 配置

### 1. 启用插件

在 AstrBot WebUI 的插件管理页面启用"大小姐管家模式"插件。

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
      tools: null  # null = 拥有所有工具，或指定工具列表如 ["web_search", "python"]
```

### 3. （可选）为管家指定专属模型

如果希望管家使用不同的模型（如更强大的模型），可以指定 `provider_id`：

```yaml
subagent_orchestrator:
  agents:
    - name: butler
      enabled: true
      provider_id: "openai_gpt4"  # 指定已配置的 provider ID
      system_prompt: |
        ...
      tools: null
```

## 工作流程

```
用户消息 → 大小姐模型（自然语言对话）
                ↓
        需要执行操作？
                ↓ 是
        transfer_to_butler
                ↓
        管家 SubAgent（双通道输入）
                ↓
        执行工具/Bash
                ↓
        自然语言汇报 → 大小姐 → 用户
```

## 注意事项

1. **必须配置 butler subagent**：本插件依赖名为 `butler` 的 subagent，请确保已在配置中正确设置
2. **核心补丁**：为了实现双通道输入，需要修改 AstrBot 核心文件 `astr_agent_tool_exec.py`（详见下方）
3. **兼容性**：本插件要求 AstrBot >= 4.16

## 核心补丁（双通道输入注入）

为了让管家能够同时获取原始用户输入和大小姐的要求，需要对 AstrBot 核心进行最小补丁：

**文件**：`astrbot/core/astr_agent_tool_exec.py`

**修改位置**：`_execute_handoff` 方法中，约第 248 行

```python
# 原代码
input_ = tool_args.get("input")

# 新增代码块（在原代码后添加）
# === 大小姐管家模式：双通道输入注入 ===
event = run_context.context.event
raw_user_input = event.get_extra("_maid_agent_raw_input")
if raw_user_input and tool.name == "transfer_to_butler":
    input_ = f"""【用户原始输入】
{raw_user_input}

【大小姐的要求】
{input_}

请综合考虑以上信息完成任务。"""
# === 双通道输入注入结束 ===
```

## 开发

```bash
# 安装开发依赖
pip install ruff

# 代码检查
ruff check .

# 代码格式化
ruff format .
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
