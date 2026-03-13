"""
大小姐管家模式插件 - 配置读取
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import CALL_MAID_TAG_NAME, DEFAULT_MAID_AGENT_NAME

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.star.context import Context


@dataclass(slots=True)
class MaidModeConfig:
    default_agent_name: str = DEFAULT_MAID_AGENT_NAME
    allowed_agent_names: list[str] | None = None
    call_tag_name: str = CALL_MAID_TAG_NAME
    include_raw_user_input: bool = True


def load_maid_mode_config(context: "Context", event: "AstrMessageEvent") -> MaidModeConfig:
    """从 AstrBot 配置中读取 maid_mode 配置。"""
    root_cfg = context.get_config(umo=event.unified_msg_origin)
    cfg = root_cfg.get("maid_mode", {}) if isinstance(root_cfg, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    default_agent_name = str(cfg.get("default_agent_name", DEFAULT_MAID_AGENT_NAME)).strip()
    if not default_agent_name:
        default_agent_name = DEFAULT_MAID_AGENT_NAME

    allowed = cfg.get("allowed_agent_names", [default_agent_name])
    if not isinstance(allowed, list):
        allowed = [default_agent_name]
    allowed_agent_names = [str(item).strip() for item in allowed if str(item).strip()]
    if default_agent_name not in allowed_agent_names:
        allowed_agent_names.append(default_agent_name)

    call_tag_name = str(cfg.get("call_tag_name", CALL_MAID_TAG_NAME)).strip() or CALL_MAID_TAG_NAME
    include_raw_user_input = bool(cfg.get("include_raw_user_input", True))

    return MaidModeConfig(
        default_agent_name=default_agent_name,
        allowed_agent_names=allowed_agent_names,
        call_tag_name=call_tag_name,
        include_raw_user_input=include_raw_user_input,
    )
