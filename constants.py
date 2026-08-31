"""
大小姐管家模式插件 - 常量定义
"""

PLUGIN_DATA_DIR_NAME = "astrbot_plugin_maid_agent"
CALL_MAID_TOOL_NAME = "call_maid"
MAID_TASK_TOOL_NAME = "maid_task"
DEFAULT_MAID_AGENT_NAME = "butler"

RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"
TRUE_USER_INPUT_EXTRA_KEY = "_maid_agent_true_user_input"

USER_INPUT_BLOCK_LABEL = "对方原话"
MISTRESS_REQUEST_BLOCK_LABEL = "大小姐请求"
MAID_NOTIFICATION_ID_META_KEY = "_maid_notification_id"
MAID_NOTIFICATION_IDS_META_KEY = "_maid_notification_ids"

DEFAULT_UMO = "dashboard:FriendMessage:dashboard"
_LEGACY_WEBID_UMO = "dashboard:WebId:dashboard"


def normalize_umo(umo: str | None) -> str:
    """空值落到控制台默认来源；2.0 初版写出的 WebId 来源非法，归一到默认值。"""
    value = (umo or "").strip()
    if not value or value == _LEGACY_WEBID_UMO:
        return DEFAULT_UMO
    return value

