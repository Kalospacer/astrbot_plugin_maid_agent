"""
大小姐管家模式插件 - 配置读取
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .constants import DEFAULT_MAID_AGENT_NAME

DEFAULT_SESSION_TIMEOUT_MINUTES = 20
DEFAULT_DISPATCH_PROMPT_TEMPLATE = (
    "{user_input_block}"
    "{maid_full_reply_block}"
    "{maid_request_block}"
    "你是MuiceMaid，一个全能的管家AIagent助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。"
    "你需要综合考虑大小姐和对方的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断对方的需求，和大小姐的意图，"
    "如果大小姐误解了对方的需求，你以对方的需求为准完成任务，如果大小姐拒绝了对方的请求，你应当停止工作并汇报结束，"
    "如果大小姐和对方的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是对方。"
)


@dataclass(slots=True)
class MaidModeConfig:
    default_agent_name: str = DEFAULT_MAID_AGENT_NAME
    allowed_agent_names: list[str] | None = None
    hide_native_tools: bool = True
    hide_transfer_tools: bool = True
    include_raw_user_input: bool = True
    session_enabled: bool = True
    log_raw_llm_io: bool = False
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES
    dispatch_prompt_template: str = DEFAULT_DISPATCH_PROMPT_TEMPLATE


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def load_maid_mode_config(config: Mapping[str, Any] | None = None) -> MaidModeConfig:
    """从插件注入配置中读取 maid agent 配置。"""
    cfg = dict(config or {})

    default_agent_name = str(cfg.get("default_agent_name", DEFAULT_MAID_AGENT_NAME)).strip()
    if not default_agent_name:
        default_agent_name = DEFAULT_MAID_AGENT_NAME

    allowed = cfg.get("allowed_agent_names", [default_agent_name])
    if not isinstance(allowed, (list, tuple, set)):
        allowed = [default_agent_name]
    allowed_agent_names: list[str] = []
    seen_agent_names: set[str] = set()
    for item in allowed:
        normalized = str(item).strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen_agent_names:
            continue
        seen_agent_names.add(key)
        allowed_agent_names.append(normalized)
    if default_agent_name.casefold() not in seen_agent_names:
        allowed_agent_names.append(default_agent_name)

    hide_native_tools = _parse_bool(cfg.get("hide_native_tools", True), True)
    hide_transfer_tools = _parse_bool(cfg.get("hide_transfer_tools", True), True)
    include_raw_user_input = _parse_bool(cfg.get("include_raw_user_input", True), True)
    session_enabled = _parse_bool(cfg.get("session_enabled", True), True)
    log_raw_llm_io = _parse_bool(cfg.get("log_raw_llm_io", False), False)
    dispatch_prompt_template = str(
        cfg.get("dispatch_prompt_template", DEFAULT_DISPATCH_PROMPT_TEMPLATE)
    )
    if not dispatch_prompt_template.strip():
        dispatch_prompt_template = DEFAULT_DISPATCH_PROMPT_TEMPLATE

    timeout_raw = cfg.get("session_timeout_minutes", DEFAULT_SESSION_TIMEOUT_MINUTES)
    try:
        session_timeout_minutes = int(timeout_raw)
    except (TypeError, ValueError):
        session_timeout_minutes = DEFAULT_SESSION_TIMEOUT_MINUTES
    if session_timeout_minutes <= 0:
        session_timeout_minutes = DEFAULT_SESSION_TIMEOUT_MINUTES

    return MaidModeConfig(
        default_agent_name=default_agent_name,
        allowed_agent_names=allowed_agent_names,
        hide_native_tools=hide_native_tools,
        hide_transfer_tools=hide_transfer_tools,
        include_raw_user_input=include_raw_user_input,
        session_enabled=session_enabled,
        log_raw_llm_io=log_raw_llm_io,
        session_timeout_minutes=session_timeout_minutes,
        dispatch_prompt_template=dispatch_prompt_template,
    )
