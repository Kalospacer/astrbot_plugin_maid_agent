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

# dispatch_prompt 中标注「用户原话 / 大小姐请求」的区块标签，
# 拼接与控制台兜底解析共用同一份，避免魔法串重复。
USER_INPUT_BLOCK_LABEL = "对方原话"
MISTRESS_REQUEST_BLOCK_LABEL = "大小姐请求"
# Notification dedup markers written into conversation history messages.
# Singular form is set when exactly one notification is delivered; plural form
# (a list) is always set. Both are scanned for dedup.
MAID_NOTIFICATION_ID_META_KEY = "_maid_notification_id"
MAID_NOTIFICATION_IDS_META_KEY = "_maid_notification_ids"

# 控制台默认消息来源。MessageType 必须是合法枚举值
# （MessageSession.from_str 会 MessageType(...) 强转，非法值直接 ValueError）。
DEFAULT_UMO = "dashboard:FriendMessage:dashboard"
_LEGACY_WEBID_UMO = "dashboard:WebId:dashboard"


def normalize_umo(umo: str | None) -> str:
    """空值落到控制台默认来源；2.0 初版写出的 WebId 来源非法，归一到默认值。"""
    value = (umo or "").strip()
    if not value or value == _LEGACY_WEBID_UMO:
        return DEFAULT_UMO
    return value

