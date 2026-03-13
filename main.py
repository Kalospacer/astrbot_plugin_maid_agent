"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 主模型不直接暴露任何原生工具
- 需要幕后执行时通过 `<call_maid>` XML 协议表达意图
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Star, register
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    ToolCall,
    ToolCallMessageSegment,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import ToolCallsResult

from .config import load_maid_mode_config
from .constants import RAW_INPUT_EXTRA_KEY
from .context_sanitizer import sanitize_contexts
from .maid_call_parser import parse_maid_call, parse_maid_session_done
from .maid_dispatcher import dispatch_to_maid_agent
from .output_sanitizer import sanitize_user_visible_output
from .prompt_injector import inject_maid_system_prompt
from .response_validator import validate_llm_response
from .session_store import MaidSessionStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context


@register(
    "MaidAgent",
    "大小姐管家模式",
    "主模型仅保留自然语言上下文，通过 XML 协议请求幕后执行",
    "1.0.0",
)
class MaidAgent(Star):
    """大小姐管家模式插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.maid_mode_config = load_maid_mode_config(self.config)
        self.session_store: MaidSessionStore | None = None

    async def initialize(self) -> None:
        """插件初始化"""
        self.session_store = MaidSessionStore(self, self.maid_mode_config)
        logger.info(
            "[MaidAgent] 已加载 | default_agent=%s | allowed_agents=%s | call_tag=%s | done_tag=%s | include_raw_user_input=%s | session_enabled=%s | session_timeout_minutes=%s",
            self.maid_mode_config.default_agent_name,
            ",".join(self.maid_mode_config.allowed_agent_names or []),
            self.maid_mode_config.call_tag_name,
            self.maid_mode_config.done_tag_name,
            self.maid_mode_config.include_raw_user_input,
            self.maid_mode_config.session_enabled,
            self.maid_mode_config.session_timeout_minutes,
        )

    def _rewrite_response_text(self, resp: LLMResponse, text: str) -> None:
        """以兼容 AstrBot 的方式回写响应文本。"""
        if resp.result_chain is None:
            resp.result_chain = MessageChain(chain=[Comp.Plain(text)])
        resp.completion_text = text

    def _replace_response(self, target: LLMResponse, source: LLMResponse) -> None:
        if source.result_chain is not None:
            target.result_chain = source.result_chain
        target.completion_text = source.completion_text or ""
        target.tools_call_name = list(source.tools_call_name or [])
        target.tools_call_args = list(source.tools_call_args or [])
        target.tools_call_ids = list(source.tools_call_ids or [])
        target.tools_call_extra_content = dict(source.tools_call_extra_content or {})
        target.reasoning_content = source.reasoning_content
        target.reasoning_signature = source.reasoning_signature

    @staticmethod
    def _contains_agent_name(agent_names: list[str] | None, agent_name: str) -> bool:
        if not agent_names:
            return False
        target = agent_name.strip().casefold()
        return any(name.strip().casefold() == target for name in agent_names)

    @staticmethod
    def _dump_json(data) -> str:
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return repr(data)

    @staticmethod
    def _is_provider_request_like(req: object) -> bool:
        return all(
            hasattr(req, attr)
            for attr in (
                "prompt",
                "image_urls",
                "contexts",
                "system_prompt",
                "model",
                "extra_user_content_parts",
            )
        )

    @filter.on_llm_request()
    async def sanitize_main_model_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """
        清洗主模型请求，实现大小姐模式。

        1. 保存原始用户输入到 event.extra
        2. 清洗 contexts - 过滤 tool role 和 tool_calls
        3. 禁用主模型原生工具
        4. 注入大小姐 XML 协议说明
        """
        raw_input = req.prompt or event.message_str or ""
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input)
            logger.debug(f"[大小姐模式] 已保存原始用户输入: {raw_input[:100]}...")

        removed_count = sanitize_contexts(req)
        if removed_count > 0:
            logger.debug(f"[大小姐模式] 已清洗 contexts，移除 {removed_count} 条非自然语言消息")

        req.func_tool = None
        logger.debug("[大小姐模式] 已禁用主模型原生工具暴露")

        if inject_maid_system_prompt(
            req,
            self.maid_mode_config.call_tag_name,
            self.maid_mode_config.default_agent_name,
            self.maid_mode_config.done_tag_name,
        ):
            logger.debug("[大小姐模式] 已注入 XML 调度协议说明")

        logger.debug(
            "[大小姐模式] LLM请求原文:\n%s",
            self._dump_json(
                {
                    "prompt": req.prompt,
                    "system_prompt": req.system_prompt,
                    "contexts": req.contexts,
                    "image_urls": req.image_urls,
                    "func_tool": (
                        [tool.name for tool in req.func_tool.tools]
                        if req.func_tool and getattr(req.func_tool, "tools", None)
                        else None
                    ),
                    "session_id": req.session_id,
                    "model": req.model,
                }
            ),
        )

    async def _request_maid_follow_up(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        maid_visible_text: str,
        agent_name: str,
        maid_request: str,
        agent_result: str,
    ) -> LLMResponse:
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到用于大小姐追答的 provider: {provider_id}")

        tool_call_id = f"maid_{uuid.uuid4().hex}"
        assistant_parts = [TextPart(text=maid_visible_text)] if maid_visible_text.strip() else None
        tool_calls_result = ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                content=assistant_parts,
                tool_calls=[
                    ToolCall(
                        id=tool_call_id,
                        function=ToolCall.FunctionBody(
                            name=f"transfer_to_{agent_name}",
                            arguments=json.dumps({"input": maid_request}, ensure_ascii=False),
                        ),
                    )
                ],
            ),
            tool_calls_result=[
                ToolCallMessageSegment(
                    tool_call_id=tool_call_id,
                    content=agent_result,
                )
            ],
        )
        return await provider.text_chat(
            prompt=req.prompt,
            image_urls=req.image_urls,
            func_tool=None,
            contexts=req.contexts,
            system_prompt=req.system_prompt,
            tool_calls_result=tool_calls_result,
            model=req.model,
            extra_user_content_parts=req.extra_user_content_parts,
        )

    @filter.on_llm_response()
    async def sanitize_llm_response(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """
        解析 XML 调度标签，并在本阶段打通子 agent 调度与回灌闭环。
        """
        native_tools = validate_llm_response(resp)
        if native_tools:
            logger.debug(f"[大小姐模式] 观测到主模型残留原生工具调用倾向: {native_tools}")

        logger.debug(
            "[大小姐模式] LLM响应原文:\n%s",
            self._dump_json(
                {
                    "completion_text": resp.completion_text,
                    "tools_call_name": resp.tools_call_name,
                    "tools_call_args": resp.tools_call_args,
                    "tools_call_ids": resp.tools_call_ids,
                    "tools_call_extra_content": resp.tools_call_extra_content,
                    "reasoning_content": resp.reasoning_content,
                }
            ),
        )

        cfg = self.maid_mode_config
        completion_text = resp.completion_text or ""
        session_done_requested = parse_maid_session_done(completion_text, cfg.done_tag_name)

        maid_call = parse_maid_call(completion_text, cfg.call_tag_name)
        if not maid_call and session_done_requested and self.session_store:
            await self.session_store.close_active_session(event.unified_msg_origin, status="done")

        if not maid_call:
            sanitized = sanitize_user_visible_output(
                completion_text,
                cfg.call_tag_name,
                cfg.done_tag_name,
            )
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            return

        agent_name = maid_call.agent_name or cfg.default_agent_name
        if cfg.allowed_agent_names and not self._contains_agent_name(
            cfg.allowed_agent_names, agent_name
        ):
            logger.warning(f"[大小姐模式] XML 请求的目标 agent 不在白名单中: {agent_name}")
            agent_name = cfg.default_agent_name

        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY, "") or ""
        image_urls_raw = getattr(getattr(event, "message_obj", None), "image_urls", None)

        logger.debug(
            f"[大小姐模式] 检测到 <{cfg.call_tag_name}>，目标 agent={agent_name}，"
            f"请求摘要: {maid_call.request_text[:100]}..."
        )

        req = event.get_extra("provider_request")
        if not self._is_provider_request_like(req):
            logger.error("[大小姐模式] event.extra['provider_request'] 不存在或类型错误")
            return

        try:
            if self.session_store is None:
                raise RuntimeError("session_store 尚未初始化")
            agent_result, resolved_agent_name = await dispatch_to_maid_agent(
                context=self.context,
                event=event,
                session_store=self.session_store,
                agent_name=agent_name,
                maid_full_reply=completion_text,
                maid_request=maid_call.request_text,
                raw_user_input=raw_input if cfg.include_raw_user_input else None,
                image_urls_raw=image_urls_raw,
            )
        except Exception as exc:
            logger.error(f"[大小姐模式] 子 agent 调度失败: {exc!s}", exc_info=True)
            agent_result = f"执行过程中出现问题：{exc!s}"
            resolved_agent_name = agent_name

        maid_visible_text = sanitize_user_visible_output(
            completion_text,
            cfg.call_tag_name,
            cfg.done_tag_name,
        )
        follow_up_resp = await self._request_maid_follow_up(
            event=event,
            req=req,
            maid_visible_text=maid_visible_text,
            agent_name=resolved_agent_name,
            maid_request=maid_call.request_text,
            agent_result=agent_result,
        )
        self._replace_response(resp, follow_up_resp)
        if session_done_requested and self.session_store:
            await self.session_store.close_active_session(event.unified_msg_origin, status="done")

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """
        结果装饰阶段。

        当前仅用于日志记录；最终用户输出清洗已在响应阶段执行。
        """
        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY)
        if raw_input:
            logger.debug(f"[大小姐模式] 本轮对话原始输入: {raw_input[:100]}...")
