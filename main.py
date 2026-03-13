"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 所有工具调用通过 handoff 转交管家 subagent 执行
- 管家同时获取原始用户输入与大小姐的自然语言要求
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import Star, logger, register
from astrbot.api.event import filter

from .constants import BUTLER_HANDOFF_TOOL_NAME, MAID_SYSTEM_PROMPT_APPEND, RAW_INPUT_EXTRA_KEY
from .context_sanitizer import sanitize_contexts
from .prompt_injector import inject_maid_system_prompt
from .response_validator import validate_llm_response
from .tool_manager import trim_tools_to_butler_handoff

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest


@register(
    "MaidAgent",
    "大小姐管家模式",
    "主模型仅保留自然语言上下文，所有工具调用通过管家subagent执行",
    "1.0.0",
)
class MaidAgent(Star):
    """大小姐管家模式插件"""

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info("大小姐管家模式插件已加载")

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
        3. 裁剪 func_tool - 仅保留管家 handoff
        4. 注入大小姐模式说明
        """
        # 1. 保存原始用户输入
        raw_input = req.prompt or event.message_str or ""
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input)
            logger.debug(f"[大小姐模式] 已保存原始用户输入: {raw_input[:100]}...")

        # 2. 清洗 contexts
        removed_count = sanitize_contexts(req)
        if removed_count > 0:
            logger.debug(
                f"[大小姐模式] 已清洗 contexts，移除 {removed_count} 条非自然语言消息"
            )

        # 3. 裁剪工具集
        if trim_tools_to_butler_handoff(req, self.context):
            logger.debug(f"[大小姐模式] 已裁剪工具集，仅保留: {BUTLER_HANDOFF_TOOL_NAME}")
        else:
            logger.warning(
                f"[大小姐模式] 未找到管家 handoff 工具: {BUTLER_HANDOFF_TOOL_NAME}，"
                "请确保已在配置中启用 butler subagent"
            )

        # 4. 注入大小姐模式说明
        if inject_maid_system_prompt(req):
            logger.debug("[大小姐模式] 已注入大小姐模式说明")

    @filter.on_llm_response()
    async def sanitize_llm_response(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """
        清洗 LLM 响应，确保大小姐模式的输出符合预期。

        主要用于记录和调试，不修改响应内容。
        """
        non_butler_tools = validate_llm_response(resp)
        if non_butler_tools:
            logger.warning(
                f"[大小姐模式] 主模型尝试直接调用非管家工具: {non_butler_tools}"
            )
        elif resp.tools_call_name:
            logger.debug(
                f"[大小姐模式] 主模型正确调用管家 handoff: {resp.tools_call_name}"
            )

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """
        结果装饰阶段，可用于最终的输出生成。

        目前仅用于日志记录。
        """
        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY)
        if raw_input:
            logger.debug(f"[大小姐模式] 本轮对话原始输入: {raw_input[:100]}...")
