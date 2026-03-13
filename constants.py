"""
大小姐管家模式插件 - 常量定义
"""

# 插件专用 key，用于存储原始用户输入
RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"

# 管家 handoff 工具名称
BUTLER_HANDOFF_TOOL_NAME = "transfer_to_butler"

# 大小姐模式系统提示追加
MAID_SYSTEM_PROMPT_APPEND = """
- 需要执行任何操作（如搜索、查询、调用工具、运行代码等）时，你可以使用神奇妙妙工具 transfer_to_butler 工具将任务转交给管家处理
- 你只需要用自然语言表达你的需求，管家会为你完成所有执行工作
- 执行完成后，管家会用自然语言向你汇报结果
"""
