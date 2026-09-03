"""
大小姐管家模式插件 - 常量定义
"""

PLUGIN_DATA_DIR_NAME = "astrbot_plugin_maid_agent"
MAID_AGENT_TOOL_NAME = "maid_agent"
MAID_SEND_MESSAGE_TOOL_NAME = "maid_send_message"
MAID_LIST_AGENTS_TOOL_NAME = "maid_list_agents"
MAID_TASK_OUTPUT_TOOL_NAME = "maid_task_output"
MAID_TASK_STOP_TOOL_NAME = "maid_task_stop"
MAID_TOOL_NAMES = frozenset(
    {
        MAID_AGENT_TOOL_NAME,
        MAID_SEND_MESSAGE_TOOL_NAME,
        MAID_LIST_AGENTS_TOOL_NAME,
        MAID_TASK_OUTPUT_TOOL_NAME,
        MAID_TASK_STOP_TOOL_NAME,
    }
)

RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"
TRUE_USER_INPUT_EXTRA_KEY = "_maid_agent_true_user_input"

USER_INPUT_BLOCK_LABEL = "对方原话"
MISTRESS_REQUEST_BLOCK_LABEL = "大小姐请求"
MAID_NOTIFICATION_ID_META_KEY = "_maid_notification_id"
MAID_NOTIFICATION_IDS_META_KEY = "_maid_notification_ids"

DASHBOARD_UMO = "dashboard:FriendMessage:dashboard"

