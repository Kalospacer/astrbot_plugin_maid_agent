"""
大小姐管家模式插件 - 常量定义
"""

# 插件专用 key，用于存储原始用户输入
RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"
REPHRASE_STAGE_EXTRA_KEY = "_maid_agent_rephrase_stage"

# XML 调度协议
CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_MAID_AGENT_NAME = "butler"

# 兼容旧实现保留的常量（Phase 1 后不再作为主路径依赖）
BUTLER_HANDOFF_TOOL_NAME = "transfer_to_butler"

# 大小姐模式系统提示追加
MAID_SYSTEM_PROMPT_APPEND = f"""
- 当你需要呼叫管家帮忙完成任务时，请在回复末尾附加 XML 块咒语：<{CALL_MAID_TAG_NAME} agent=\"{DEFAULT_MAID_AGENT_NAME}\">这里写给管家的要求</{CALL_MAID_TAG_NAME}>
- 如果不需要呼叫管家帮忙，就不要说这个咒语
- XML 标签中的内容是你对管家的任务要求
"""
