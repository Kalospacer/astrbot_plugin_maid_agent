"""
大小姐管家模式插件 - 配置读取
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_DONE_TAG_NAME = "maid_session"
DEFAULT_MAID_AGENT_NAME = "butler"
DEFAULT_SESSION_TIMEOUT_MINUTES = 20
DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE = (
    "- 当你需要呼叫管家帮忙完成任务时，请在回复末尾附加 XML 块咒语："
    '<{call_tag_name} agent="{default_agent_name}">这里写给管家的要求</{call_tag_name}>'
    "\n- 如果不需要呼叫管家帮忙，就不要说这个咒语"
    "\n- XML 标签中的内容是你对管家的任务要求\n"
    '- 当你判断当前管家任务已经结束时，请额外附加独立结束标签：<{done_tag_name} status="done" />'
    "\n- 如果当前管家任务尚未结束，就不要输出结束标签\n"
)
DEFAULT_DISPATCH_PROMPT_TEMPLATE = (
    "{user_input_block}"
    "{maid_full_reply_block}"
    "{maid_request_block}"
    "你是MuiceMaid，一个全能的管家AIagent助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。"
    "你需要综合考虑大小姐和用户的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断用户的需求，和大小姐的意图，"
    "如果大小姐误解了用户的需求，你以用户的需求为准完成任务，如果大小姐拒绝了用户的请求，你应当停止工作并汇报结束，"
    "如果大小姐和用户的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是用户。"
)


@dataclass(slots=True)
class MaidModeConfig:
    default_agent_name: str = DEFAULT_MAID_AGENT_NAME
    allowed_agent_names: list[str] | None = None
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME
    done_tag_name: str = DEFAULT_DONE_TAG_NAME
    include_raw_user_input: bool = True
    session_enabled: bool = True
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES
    main_system_prompt_template: str = DEFAULT_MAIN_SYSTEM_PROMPT_TEMPLATE
    dispatch_prompt_template: str = DEFAULT_DISPATCH_PROMPT_TEMPLATE


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
    done_tag_name = (
        str(cfg.get("done_tag_name", DEFAULT_DONE_TAG_NAME)).strip()
        or DEFAULT_DONE_TAG_NAME
    )
    include_raw_user_input = bool(cfg.get("include_raw_user_input", True))
    session_enabled = bool(cfg.get("session_enabled", True))
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
        call_tag_name=call_tag_name,
        done_tag_name=done_tag_name,
        include_raw_user_input=include_raw_user_input,
        session_enabled=session_enabled,
        session_timeout_minutes=session_timeout_minutes,
        main_system_prompt_template=main_system_prompt_template,
        dispatch_prompt_template=dispatch_prompt_template,
    )
