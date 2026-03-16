"""
大小姐管家模式插件 - 常量定义
"""

# 插件目录名/数据目录名
PLUGIN_DATA_DIR_NAME = "astrbot_plugin_maid_agent"
DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_MAID_AGENT_NAME = "butler"

# 插件专用 key，用于存储原始用户输入
RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"
TRUE_USER_INPUT_EXTRA_KEY = "_maid_agent_true_user_input"
PENDING_MAID_FOLLOW_UP_EXTRA_KEY = "_maid_agent_pending_follow_up"
INTERNAL_SEND_KIND_EXTRA_KEY = "_maid_agent_internal_send_kind"

# Session 持久化
ACTIVE_SESSION_INDEX_KEY = "active_sessions_v1"
SERVING_ENABLED_KEY_PREFIX = "maid_serve_enabled_v1::"
