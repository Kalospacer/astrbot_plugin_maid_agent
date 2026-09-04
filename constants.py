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

# 派发后必须让主模型收尾：主 agent 的 run 要等它不再调工具、吐出最终文本才结束，
# run 活着期间 AstrBot 会把用户的新消息捕获成 follow-up 吞进 tool result。
DISPATCHED_NEXT_STEP = (
    "Dispatched. Stop calling tools now and reply to the user in this same turn. "
    "The maid narrates its own progress to the user and its final report is "
    "delivered back to you automatically as a new turn — you never fetch it."
)
RUNNING_NEXT_STEP = (
    "Still running. Report this progress to the user and end your turn. Calling "
    "this again does not make the maid finish sooner; it only keeps your turn "
    "open, and anything the user says meanwhile gets swallowed instead of answered."
)

