"""
大小姐管家模式插件 - 子 agent 调度器
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.core.agent.message import Message
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.agent.handoff import HandoffTool
    from astrbot.core.star.context import Context


def _list_handoffs(context: "Context") -> list["HandoffTool"]:
    orchestrator = getattr(context, "subagent_orchestrator", None)
    handoffs = getattr(orchestrator, "handoffs", None) or []
    return [handoff for handoff in handoffs if getattr(handoff, "agent", None) is not None]


def _find_handoff(context: "Context", agent_name: str) -> "HandoffTool | None":
    target_name = agent_name.strip().casefold()
    for handoff in _list_handoffs(context):
        handoff_name = getattr(getattr(handoff, "agent", None), "name", None)
        if isinstance(handoff_name, str) and handoff_name.strip().casefold() == target_name:
            return handoff
    return None


def _resolve_handoff(context: "Context", agent_name: str) -> tuple["HandoffTool", str]:
    handoff = _find_handoff(context, agent_name)
    if handoff is not None:
        resolved_name = getattr(getattr(handoff, "agent", None), "name", None) or agent_name
        return handoff, str(resolved_name)

    handoffs = _list_handoffs(context)
    if handoffs:
        fallback = handoffs[0]
        fallback_name = getattr(getattr(fallback, "agent", None), "name", None) or agent_name
        logger.warning(
            "[大小姐模式] 未找到名为 %s 的子 agent，已回退到第一个可用子 agent: %s",
            agent_name,
            fallback_name,
        )
        return fallback, str(fallback_name)

    raise ValueError("未找到任何可用的子 agent")


def _build_dispatch_prompt(
    raw_user_input: str | None,
    maid_full_reply: str,
    maid_request: str | None = None,
) -> str:
    parts: list[str] = []
    if raw_user_input and raw_user_input.strip():
        parts.append(f"【用户原始输入】\n{raw_user_input.strip()}")
    parts.append(f"【大小姐完整回复】\n{maid_full_reply.strip()}")
    if maid_request and maid_request.strip():
        parts.append(f"【大小姐显式请求】\n{maid_request.strip()}")
    parts.append(
        "你是MuiceMaid，一个全能的管家AIagent助手，擅长从大小姐的话语中理解大小姐的意图，并提取出大小姐的需求主动完成大小姐的愿望。你需要综合考虑大小姐和用户的对话，提取他们是否需要执行某些实际操作，并综合以上信息完成任务，请判断用户的需求，和大小姐的意图，如果大小姐误解了用户的需求，你以用户的需求为准完成任务，如果大小姐拒绝了用户的请求，你应当停止工作并汇报结束，如果大小姐和用户的需求一致，结合两者的需求准确完成任务。你的汇报对象是大小姐，不是用户。"
    )
    return "\n\n".join(parts)


def _normalize_begin_dialogs(dialogs: Any) -> list[Message] | None:
    if not dialogs:
        return None

    contexts: list[Message] = []
    for dialog in dialogs:
        try:
            contexts.append(
                dialog if isinstance(dialog, Message) else Message.model_validate(dialog)
            )
        except Exception:
            continue
    return contexts or None


def _load_provider_settings(context: "Context", event: "AstrMessageEvent") -> dict[str, Any]:
    root_cfg = context.get_config(umo=event.unified_msg_origin)
    if not isinstance(root_cfg, dict):
        return {}
    provider_settings = root_cfg.get("provider_settings", {})
    return provider_settings if isinstance(provider_settings, dict) else {}


async def dispatch_to_maid_agent(
    context: "Context",
    event: "AstrMessageEvent",
    agent_name: str,
    maid_full_reply: str,
    maid_request: str,
    raw_user_input: str | None,
    image_urls_raw: Any = None,
) -> tuple[str, str]:
    """根据 agent 名调用对应子 agent，并返回其自然语言结果与实际命中的 agent 名。"""
    handoff, resolved_agent_name = _resolve_handoff(context, agent_name)
    logger.debug("[大小姐模式] 本次调度实际使用子 agent: %s", resolved_agent_name)

    agent_context = AstrAgentContext(context=context, event=event)
    run_context = AgentContextWrapper(context=agent_context, tool_call_timeout=60)

    toolset = FunctionToolExecutor._build_handoff_toolset(run_context, handoff.agent.tools)
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(run_context, image_urls_raw)

    provider_id = getattr(
        handoff, "provider_id", None
    ) or await context.get_current_chat_provider_id(event.unified_msg_origin)
    dispatch_prompt = _build_dispatch_prompt(
        raw_user_input=raw_user_input,
        maid_full_reply=maid_full_reply,
        maid_request=maid_request,
    )
    begin_dialogs = _normalize_begin_dialogs(getattr(handoff.agent, "begin_dialogs", None))

    provider_settings = _load_provider_settings(context, event)
    agent_max_step = int(provider_settings.get("max_agent_step", 30))
    stream = bool(provider_settings.get("streaming_response", False))

    llm_resp = await context.tool_loop_agent(
        event=event,
        chat_provider_id=provider_id,
        prompt=dispatch_prompt,
        image_urls=image_urls,
        system_prompt=handoff.agent.instructions,
        tools=toolset,
        contexts=begin_dialogs,
        max_steps=agent_max_step,
        stream=stream,
        agent_context=agent_context,
        tool_call_timeout=60,
    )
    return llm_resp.completion_text or "", resolved_agent_name
