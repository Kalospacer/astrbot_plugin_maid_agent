"""
大小姐管家模式插件 - 配置读取
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_MAID_AGENT_NAME = "butler"


@dataclass(slots=True)
class MaidModeConfig:
    default_agent_name: str = DEFAULT_MAID_AGENT_NAME
    allowed_agent_names: list[str] | None = None
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME
    include_raw_user_input: bool = True


def load_maid_mode_config(config: Mapping[str, Any] | None = None) -> MaidModeConfig:
    """从插件注入配置中读取 maid agent 配置。"""
    cfg = dict(config or {})

    default_agent_name = str(cfg.get("default_agent_name", DEFAULT_MAID_AGENT_NAME)).strip()
    if not default_agent_name:
        default_agent_name = DEFAULT_MAID_AGENT_NAME

    allowed = cfg.get("allowed_agent_names", [default_agent_name])
    if not isinstance(allowed, list):
        allowed = [default_agent_name]
    allowed_agent_names = [str(item).strip() for item in allowed if str(item).strip()]
    if default_agent_name not in allowed_agent_names:
        allowed_agent_names.append(default_agent_name)

    call_tag_name = (
        str(cfg.get("call_tag_name", DEFAULT_CALL_MAID_TAG_NAME)).strip()
        or DEFAULT_CALL_MAID_TAG_NAME
    )
    include_raw_user_input = bool(cfg.get("include_raw_user_input", True))

    return MaidModeConfig(
        default_agent_name=default_agent_name,
        allowed_agent_names=allowed_agent_names,
        call_tag_name=call_tag_name,
        include_raw_user_input=include_raw_user_input,
    )
