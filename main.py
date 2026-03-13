"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 主模型不直接暴露任何原生工具
- 需要幕后执行时通过 `<call_maid>` XML 协议表达意图
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import astrbot.core.message_components as Comp
from astrbot.api import Star, logger, register
from astrbot.api.event import filter
from astrbot.core.message.message_event_result import MessageChain

from .config import load_maid_mode_config
from .constants import RAW_INPUT_EXTRA_KEY, REPHRASE_STAGE_EXTRA_KEY
from .context_sanitizer import sanitize_contexts
from .maid_call_parser import parse_maid_call
from .maid_dispatcher import dispatch_to_maid_agent
from .maid_result_rewriter import build_maid_rephrase_prompt
from .output_sanitizer import sanitize_user_visible_output
from .prompt_injector import inject_maid_system_prompt
from .response_validator import validate_llm_response

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest


@register(
    "MaidAgent",
    "大小姐管家模式",
    "主模型仅保留自然语言上下文，通过 XML 协议请求幕后执行",
    "1.0.0",
)
class MaidAgent(Star):
    """大小姐管家模式插件"""

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info("大小姐管家模式插件已加载（XML 协议模式）")

    def _rewrite_response_text(self, resp: LLMResponse, text: str) -> None:
        """以兼容 AstrBot 的方式回写响应文本。"""
        if resp.result_chain is None:
            resp.result_chain = MessageChain(chain=[Comp.Plain(text)])
        resp.completion_text = text

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
        if event.get_extra(REPHRASE_STAGE_EXTRA_KEY, False):
            req.func_tool = None
            return

        raw_input = req.prompt or event.message_str or ""
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input)
            logger.debug(f"[大小姐模式] 已保存原始用户输入: {raw_input[:100]}...")

        removed_count = sanitize_contexts(req)
        if removed_count > 0:
            logger.debug(
                f"[大小姐模式] 已清洗 contexts，移除 {removed_count} 条非自然语言消息"
            )

        req.func_tool = None
        logger.debug("[大小姐模式] 已禁用主模型原生工具暴露")

        if inject_maid_system_prompt(req):
            logger.debug("[大小姐模式] 已注入 XML 调度协议说明")

    async def _request_maid_rephrase(
        self,
        event: AstrMessageEvent,
        original_user_input: str,
        maid_visible_text: str,
        agent_result: str,
    ) -> str:
        cfg = load_maid_mode_config(self.context, event)
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        prompt = build_maid_rephrase_prompt(
            original_user_input=original_user_input,
            maid_visible_text=maid_visible_text,
            agent_result=agent_result,
        )
        event.set_extra(REPHRASE_STAGE_EXTRA_KEY, True)
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="",
                tools=None,
                contexts=[],
            )
        finally:
            event.set_extra(REPHRASE_STAGE_EXTRA_KEY, False)
        return sanitize_user_visible_output(llm_resp.completion_text or "", cfg.call_tag_name)

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
            logger.debug(
                f"[大小姐模式] 观测到主模型残留原生工具调用倾向: {native_tools}"
            )

        cfg = load_maid_mode_config(self.context, event)
        completion_text = resp.completion_text or ""

        if event.get_extra(REPHRASE_STAGE_EXTRA_KEY, False):
            sanitized = sanitize_user_visible_output(completion_text, cfg.call_tag_name)
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            return

        maid_call = parse_maid_call(completion_text, cfg.call_tag_name)
        if not maid_call:
            sanitized = sanitize_user_visible_output(completion_text, cfg.call_tag_name)
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            return

        agent_name = maid_call.agent_name or cfg.default_agent_name
        if cfg.allowed_agent_names and agent_name not in cfg.allowed_agent_names:
            logger.warning(f"[大小姐模式] XML 请求的目标 agent 不在白名单中: {agent_name}")
            agent_name = cfg.default_agent_name

        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY, "") or ""
        image_urls_raw = getattr(getattr(event, "message_obj", None), "image_urls", None)

        logger.debug(
            f"[大小姐模式] 检测到 <{cfg.call_tag_name}>，目标 agent={agent_name}，"
            f"请求摘要: {maid_call.request_text[:100]}..."
        )

        try:
            agent_result = await dispatch_to_maid_agent(
                context=self.context,
                event=event,
                agent_name=agent_name,
                maid_request=maid_call.request_text,
                raw_user_input=raw_input if cfg.include_raw_user_input else None,
                image_urls_raw=image_urls_raw,
            )
        except Exception as exc:
            logger.error(f"[大小姐模式] 子 agent 调度失败: {exc!s}", exc_info=True)
            agent_result = f"执行过程中出现问题：{exc!s}"

        final_text = await self._request_maid_rephrase(
            event=event,
            original_user_input=raw_input,
            maid_visible_text=sanitize_user_visible_output(completion_text, cfg.call_tag_name),
            agent_result=agent_result,
        )
        self._rewrite_response_text(resp, final_text)

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """
        结果装饰阶段。

        当前仅用于日志记录；最终用户输出清洗已在响应阶段执行。
        """
        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY)
        if raw_input:
            logger.debug(f"[大小姐模式] 本轮对话原始输入: {raw_input[:100]}...")
