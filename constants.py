"""
大小姐管家模式插件 - 常量定义
"""

# 插件目录名/数据目录名
PLUGIN_DATA_DIR_NAME = "astrbot_plugin_maid_agent"
CALL_MAID_TOOL_NAME = "call_maid"
MAID_TASK_TOOL_NAME = "maid_task"
DEFAULT_MAID_AGENT_NAME = "butler"

# 插件专用 key，用于存储原始用户输入
RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"
TRUE_USER_INPUT_EXTRA_KEY = "_maid_agent_true_user_input"
PENDING_MAID_FOLLOW_UP_EXTRA_KEY = "_maid_agent_pending_follow_up"
PENDING_MAID_DISPATCHES_EXTRA_KEY = "_maid_agent_pending_dispatches"
PENDING_MAID_TOOL_HISTORY_EXTRA_KEY = "_maid_agent_pending_tool_history"
MAID_AGENT_ID_META_KEY = "_maid_agent_id"
MAID_NOTIFICATION_ID_META_KEY = "_maid_notification_id"

# Session 持久化（1.2.0 legacy，1.3.0 runtime 与之隔离）
ACTIVE_SESSION_INDEX_KEY = "active_sessions_v1"
