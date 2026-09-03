"""
大小姐管家模式插件 - 子 agent 调度器
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.utils.llm_metadata import LLM_METADATAS

from .config import _safe_int

_provider_config_locks: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context
    from astrbot.core.agent.handoff import HandoffTool
    from astrbot.core.provider.provider import Provider


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


def _resolve_handoff(
    context: Context,
    agent_name: str,
) -> tuple[HandoffTool, str]:
    handoff = _find_handoff(context, agent_name)
    if handoff is not None:
        resolved_name = getattr(getattr(handoff, "agent", None), "name", None) or agent_name
        return handoff, str(resolved_name)

    raise ValueError(f"未找到可用的子 agent: {agent_name}")


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


def _ensure_provider_max_context_tokens(provider: Provider) -> int:
    max_context_tokens = _safe_int(provider.provider_config.get("max_context_tokens", 0), 0)
    if max_context_tokens > 0:
        return max_context_tokens

    model = provider.get_model()
    model_info = LLM_METADATAS.get(model)
    if not model_info:
        return 0

    inferred = _safe_int(model_info.get("limit", {}).get("context", 0), 0)
    if inferred > 0:
        logger.debug(
            "[大小姐模式] 已为子 agent provider 推断 max_context_tokens: model=%s limit=%s",
            model,
            inferred,
        )
    return inferred


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
    max_context_tokens: int,
    session_id: str,
    agent_hooks=None,
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
        session_id=session_id,
    )
    provider_lock = _provider_config_locks.setdefault(provider, asyncio.Lock())
    async with provider_lock:
        original_max_context_tokens = provider.provider_config.get("max_context_tokens")
        if max_context_tokens > 0:
            provider.provider_config["max_context_tokens"] = max_context_tokens
        try:
            await runner.reset(
                provider=provider,
                request=request,
                run_context=AgentContextWrapper(
                    context=agent_context,
                    messages=[],
                    tool_call_timeout=tool_call_timeout,
                ),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=agent_hooks or BaseAgentRunHooks[AstrAgentContext](),
                streaming=stream,
                llm_compress_instruction=llm_compress_instruction,
                llm_compress_keep_recent=llm_compress_keep_recent,
                llm_compress_provider=llm_compress_provider,
                truncate_turns=truncate_turns,
                enforce_max_turns=enforce_max_turns,
                tool_schema_mode=tool_schema_mode,
            )
        finally:
            if max_context_tokens > 0:
                if original_max_context_tokens is None:
                    provider.provider_config.pop("max_context_tokens", None)
                else:
                    provider.provider_config["max_context_tokens"] = original_max_context_tokens
    return runner
