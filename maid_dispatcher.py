"""
大小姐管家模式插件 - 子 agent 调度器
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.core.agent.message import Message
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.agent.handoff import HandoffTool
    from astrbot.core.star.context import Context


def _find_handoff(context: "Context", agent_name: str) -> "HandoffTool | None":
    orchestrator = getattr(context, "subagent_orchestrator", None)
    handoffs = getattr(orchestrator, "handoffs", None) or []
    for handoff in handoffs:
        if getattr(getattr(handoff, "agent", None), "name", None) == agent_name:
            return handoff
    return None


def _build_dispatch_prompt(raw_user_input: str | None, maid_request: str) -> str:
    parts: list[str] = []
    if raw_user_input and raw_user_input.strip():
        parts.append(f"【用户原始输入】\n{raw_user_input.strip()}")
    parts.append(f"【大小姐的要求】\n{maid_request.strip()}")
    parts.append("请综合考虑以上信息完成任务。你的汇报对象是大小姐，不是用户。")
    return "\n\n".join(parts)


def _normalize_begin_dialogs(dialogs: Any) -> list[Message] | None:
    if not dialogs:
        return None

    contexts: list[Message] = []
    for dialog in dialogs:
        try:
            contexts.append(dialog if isinstance(dialog, Message) else Message.model_validate(dialog))
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
    maid_request: str,
    raw_user_input: str | None,
    image_urls_raw: Any = None,
) -> str:
    """根据 agent 名调用对应子 agent，并返回其自然语言结果。"""
    handoff = _find_handoff(context, agent_name)
    if handoff is None:
        raise ValueError(f"未找到可用的子 agent: {agent_name}")

    agent_context = AstrAgentContext(context=context, event=event)
    run_context = AgentContextWrapper(context=agent_context, tool_call_timeout=60)

    toolset = FunctionToolExecutor._build_handoff_toolset(run_context, handoff.agent.tools)
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(run_context, image_urls_raw)

    provider_id = getattr(handoff, "provider_id", None) or await context.get_current_chat_provider_id(
        event.unified_msg_origin
    )
    dispatch_prompt = _build_dispatch_prompt(raw_user_input, maid_request)
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
    return llm_resp.completion_text or ""
