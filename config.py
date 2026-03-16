"""
大小姐管家模式插件 - 配置读取
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_MAID_AGENT_NAME = "butler"
DEFAULT_SESSION_TIMEOUT_MINUTES = 20
DEFAULT_SERVING_MAX_TURNS = 3
DEFAULT_SERVING_PROMPT_TEMPLATE = "根据上文，你决定继续说话。"
DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE = (
    "- 需要管家协助时，回复末尾附加："
    '<{call_tag_name} agent="{default_agent_name}">任务要求</{call_tag_name}>'
    "\n- 不需要管家则不附加此标签"
    '\n- 停止管家任务：<{call_tag_name} action="stop" />'
    '\n- 补充或修正当前管家任务：<{call_tag_name} action="steer">补充要求</{call_tag_name}>'
    '\n- 管家任务结束时附加：<{call_tag_name} action="done" />，未结束不附加'
)
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
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME
    include_raw_user_input: bool = True
    session_enabled: bool = True
    log_raw_llm_io: bool = False
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES
    serving_mode_enabled: bool = False
    serving_max_turns: int = DEFAULT_SERVING_MAX_TURNS
    serving_prompt_template: str = DEFAULT_SERVING_PROMPT_TEMPLATE
    main_system_prompt_template: str = DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE
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


def _normalize_xml_tag_name(value: Any, default: str) -> str:
    candidate = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", candidate):
        return candidate
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

    call_tag_name = _normalize_xml_tag_name(
        cfg.get("call_tag_name", DEFAULT_CALL_MAID_TAG_NAME),
        DEFAULT_CALL_MAID_TAG_NAME,
    )
    include_raw_user_input = _parse_bool(cfg.get("include_raw_user_input", True), True)
    session_enabled = _parse_bool(cfg.get("session_enabled", True), True)
    log_raw_llm_io = _parse_bool(cfg.get("log_raw_llm_io", False), False)
    serving_mode_enabled = _parse_bool(cfg.get("serving_mode_enabled", False), False)
    main_system_prompt_template = str(
        cfg.get("main_system_prompt_template", DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE)
    )
    if not main_system_prompt_template.strip():
        main_system_prompt_template = DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE
    dispatch_prompt_template = str(
        cfg.get("dispatch_prompt_template", DEFAULT_DISPATCH_PROMPT_TEMPLATE)
    )
    if not dispatch_prompt_template.strip():
        dispatch_prompt_template = DEFAULT_DISPATCH_PROMPT_TEMPLATE
    serving_prompt_template = str(
        cfg.get("serving_prompt_template", DEFAULT_SERVING_PROMPT_TEMPLATE)
    )
    if not serving_prompt_template.strip():
        serving_prompt_template = DEFAULT_SERVING_PROMPT_TEMPLATE

    timeout_raw = cfg.get("session_timeout_minutes", DEFAULT_SESSION_TIMEOUT_MINUTES)
    try:
        session_timeout_minutes = int(timeout_raw)
    except (TypeError, ValueError):
        session_timeout_minutes = DEFAULT_SESSION_TIMEOUT_MINUTES
    if session_timeout_minutes <= 0:
        session_timeout_minutes = DEFAULT_SESSION_TIMEOUT_MINUTES
    serving_max_turns_raw = cfg.get("serving_max_turns", DEFAULT_SERVING_MAX_TURNS)
    try:
        serving_max_turns = int(serving_max_turns_raw)
    except (TypeError, ValueError):
        serving_max_turns = DEFAULT_SERVING_MAX_TURNS
    if serving_max_turns <= 0:
        serving_max_turns = DEFAULT_SERVING_MAX_TURNS

    return MaidModeConfig(
        default_agent_name=default_agent_name,
        allowed_agent_names=allowed_agent_names,
        call_tag_name=call_tag_name,
        include_raw_user_input=include_raw_user_input,
        session_enabled=session_enabled,
        log_raw_llm_io=log_raw_llm_io,
        session_timeout_minutes=session_timeout_minutes,
        serving_mode_enabled=serving_mode_enabled,
        serving_max_turns=serving_max_turns,
        serving_prompt_template=serving_prompt_template,
        main_system_prompt_template=main_system_prompt_template,
        dispatch_prompt_template=dispatch_prompt_template,
    )
