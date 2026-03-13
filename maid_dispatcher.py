"""
大小姐管家模式插件 - 子 agent 调度器
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from .session_store import MaidAgentSession, MaidSessionStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context
    from astrbot.core.agent.handoff import HandoffTool
    from astrbot.core.provider.provider import Provider


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list_handoffs(context: Context) -> list[HandoffTool]:
    orchestrator = getattr(context, "subagent_orchestrator", None)
    handoffs = getattr(orchestrator, "handoffs", None) or []
    return [handoff for handoff in handoffs if getattr(handoff, "agent", None) is not None]


def _find_handoff(context: Context, agent_name: str) -> HandoffTool | None:
    target_name = agent_name.strip().casefold()
    for handoff in _list_handoffs(context):
        handoff_name = getattr(getattr(handoff, "agent", None), "name", None)
        if isinstance(handoff_name, str) and handoff_name.strip().casefold() == target_name:
            return handoff
    return None


def _resolve_handoff(context: Context, agent_name: str) -> tuple[HandoffTool, str]:
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
    true_user_input: str | None,
    maid_full_reply: str,
    dispatch_prompt_template: str,
    maid_request: str | None = None,
) -> str:
    normalized_true_input = (true_user_input or "").strip()
    user_input_block = f"【用户原话】\n{normalized_true_input}\n\n" if normalized_true_input else ""
    maid_full_reply_block = f"【大小姐完整回复】\n{maid_full_reply.strip()}\n\n"
    maid_request_block = (
        f"【大小姐显式请求】\n{maid_request.strip()}\n\n"
        if maid_request and maid_request.strip()
        else ""
    )
    return (
        dispatch_prompt_template.replace("{user_input_block}", user_input_block)
        .replace("{maid_full_reply_block}", maid_full_reply_block)
        .replace("{maid_request_block}", maid_request_block)
        .strip()
    )


def _normalize_begin_dialogs(dialogs: Any) -> list[Message] | None:
    if not dialogs:
        return None

    contexts: list[Message] = []
    for dialog in dialogs:
        try:
            contexts.append(
                dialog if isinstance(dialog, Message) else Message.model_validate(dialog)
            )
        except Exception as exc:
            logger.warning(
                "[大小姐模式] 解析 begin_dialogs 条目失败，已跳过: %s | dialog=%r",
                exc,
                dialog,
            )
            continue
    return contexts or None


def _load_provider_settings(context: Context, event: AstrMessageEvent) -> dict[str, Any]:
    root_cfg = context.get_config(umo=event.unified_msg_origin)
    if not isinstance(root_cfg, dict):
        return {}
    provider_settings = root_cfg.get("provider_settings", {})
    return provider_settings if isinstance(provider_settings, dict) else {}


def _get_compress_provider(
    context: Context,
    provider_settings: dict[str, Any],
):
    provider_id = str(provider_settings.get("llm_compress_provider_id", "")).strip()
    strategy = str(provider_settings.get("context_limit_reached_strategy", "truncate_by_turns"))
    if not provider_id or strategy != "llm_compress":
        return None
    provider = context.get_provider_by_id(provider_id)
    if provider is None:
        logger.warning("[大小姐模式] 未找到指定的上下文压缩模型 %s，将跳过压缩。", provider_id)
        return None
    return provider


def _build_session_contexts(
    session: MaidAgentSession | None,
    begin_dialogs: list[Message] | None,
) -> list[dict[str, Any]] | list[Message] | None:
    if session and session.messages:
        messages = list(session.messages)
        if messages and messages[0].get("role") == "system":
            messages = messages[1:]
        return messages or None
    return begin_dialogs


async def _build_runner(
    *,
    context: Context,
    event: AstrMessageEvent,
    provider: Provider,
    prompt: str,
    image_urls: list[str],
    system_prompt: str,
    tools,
    contexts: list[dict[str, Any]] | list[Message] | None,
    stream: bool,
    tool_call_timeout: int,
    llm_compress_instruction: str,
    llm_compress_keep_recent: int,
    llm_compress_provider,
    truncate_turns: int,
    enforce_max_turns: int,
    tool_schema_mode: str,
) -> ToolLoopAgentRunner:
    agent_context = AstrAgentContext(context=context, event=event)
    runner = ToolLoopAgentRunner()
    request = ProviderRequest(
        prompt=prompt,
        image_urls=image_urls,
        func_tool=tools,
        contexts=[
            msg.model_dump() if isinstance(msg, Message) else msg for msg in (contexts or [])
        ],
        system_prompt=system_prompt,
        session_id=event.unified_msg_origin,
    )
    await runner.reset(
        provider=provider,
        request=request,
        run_context=AgentContextWrapper(
            context=agent_context,
            tool_call_timeout=tool_call_timeout,
        ),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks[AstrAgentContext](),
        streaming=stream,
        llm_compress_instruction=llm_compress_instruction,
        llm_compress_keep_recent=llm_compress_keep_recent,
        llm_compress_provider=llm_compress_provider,
        truncate_turns=truncate_turns,
        enforce_max_turns=enforce_max_turns,
        tool_schema_mode=tool_schema_mode,
    )
    return runner


async def dispatch_to_maid_agent(
    context: Context,
    event: AstrMessageEvent,
    session_store: MaidSessionStore,
    agent_name: str,
    maid_full_reply: str,
    maid_request: str,
    true_user_input: str | None,
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
        true_user_input=true_user_input,
        maid_full_reply=maid_full_reply,
        dispatch_prompt_template=session_store.config.dispatch_prompt_template,
        maid_request=maid_request,
    )
    begin_dialogs = _normalize_begin_dialogs(getattr(handoff.agent, "begin_dialogs", None))

    provider_settings = _load_provider_settings(context, event)
    agent_max_step = _safe_int(provider_settings.get("max_agent_step", 30), 30)
    stream = bool(provider_settings.get("streaming_response", False))
    tool_call_timeout = _safe_int(provider_settings.get("tool_call_timeout", 60), 60)
    llm_compress_instruction = str(provider_settings.get("llm_compress_instruction", "") or "")
    llm_compress_keep_recent = _safe_int(provider_settings.get("llm_compress_keep_recent", 4), 4)
    truncate_turns = _safe_int(provider_settings.get("dequeue_context_length", 1), 1)
    enforce_max_turns = _safe_int(provider_settings.get("max_context_length", -1), -1)
    tool_schema_mode = str(provider_settings.get("tool_schema_mode", "full") or "full")
    llm_compress_provider = _get_compress_provider(context, provider_settings)

    provider = context.get_provider_by_id(provider_id)
    if provider is None:
        raise RuntimeError(f"未找到子 agent provider: {provider_id}")

    session: MaidAgentSession | None = None
    if session_store.config.session_enabled:
        session, reused = await session_store.get_or_create_active_session(
            event.unified_msg_origin,
            resolved_agent_name,
        )
        if reused:
            logger.info(
                "[大小姐模式] 已续接现有管家 session: umo=%s session_id=%s",
                event.unified_msg_origin,
                session.session_id,
            )

    runner = await _build_runner(
        context=context,
        event=event,
        provider=provider,
        prompt=dispatch_prompt,
        image_urls=image_urls,
        system_prompt=handoff.agent.instructions,
        tools=toolset,
        contexts=_build_session_contexts(session, begin_dialogs),
        stream=stream,
        tool_call_timeout=tool_call_timeout,
        llm_compress_instruction=llm_compress_instruction,
        llm_compress_keep_recent=llm_compress_keep_recent,
        llm_compress_provider=llm_compress_provider,
        truncate_turns=truncate_turns,
        enforce_max_turns=enforce_max_turns,
        tool_schema_mode=tool_schema_mode,
    )
    async for _ in runner.step_until_done(agent_max_step):
        pass

    llm_resp = runner.get_final_llm_resp()
    if llm_resp is None:
        raise RuntimeError("子 agent 未返回最终响应")

    if session is not None:
        session.agent_name = resolved_agent_name
        session.messages = [msg.model_dump() for msg in runner.run_context.messages]
        session.last_maid_request = maid_request
        session.last_agent_result = llm_resp.completion_text or ""
        await session_store.save_session(session)
        logger.debug(
            "[大小姐模式] 已持久化管家 session: session_id=%s messages=%d",
            session.session_id,
            len(session.messages),
        )
    return llm_resp.completion_text or "", resolved_agent_name
