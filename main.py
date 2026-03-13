"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 所有工具调用通过 handoff 转交管家 subagent 执行
- 管家同时获取原始用户输入与大小姐的自然语言要求
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrbot.api import Star, register
from astrbot.api import filter
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.register import llm_tools

if TYPE_CHECKING:
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest

# 插件专用 key，用于存储原始用户输入
RAW_INPUT_EXTRA_KEY = "_maid_agent_raw_input"

# 管家 handoff 工具名称
BUTLER_HANDOFF_TOOL_NAME = "transfer_to_butler"

# 大小姐模式系统提示追加
MAID_SYSTEM_PROMPT_APPEND = """

【大小姐模式】
你是一位优雅的大小姐，只负责自然语言对话和理解用户意图。
- 当需要执行任何操作（如搜索、查询、调用工具、运行代码等）时，请使用 transfer_to_butler 工具将任务转交给管家处理
- 你只需要用自然语言表达你的需求，管家会为你完成所有执行工作
- 执行完成后，管家会用自然语言向你汇报结果
- 请保持优雅、礼貌的对话风格
"""

logger = logging.getLogger(__name__)


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

        # 2. 清洗 contexts - 过滤 tool role 和 tool_calls
        natural_contexts = []
        for ctx in req.contexts:
            role = ctx.get("role", "")
            # 过滤掉 tool 角色的消息
            if role == "tool":
                continue
            # 过滤掉包含 tool_calls 的 assistant 消息
            if role == "assistant" and "tool_calls" in ctx:
                continue
            natural_contexts.append(ctx)

        if len(req.contexts) != len(natural_contexts):
            logger.debug(
                f"[大小姐模式] 已清洗 contexts: {len(req.contexts)} -> {len(natural_contexts)}"
            )
        req.contexts = natural_contexts

        # 3. 裁剪 func_tool - 仅保留管家 handoff
        butler_handoff = llm_tools.get_func(BUTLER_HANDOFF_TOOL_NAME)
        if butler_handoff:
            req.func_tool = ToolSet()
            req.func_tool.add_tool(butler_handoff)
            logger.debug(f"[大小姐模式] 已裁剪工具集，仅保留: {BUTLER_HANDOFF_TOOL_NAME}")
        else:
            logger.warning(
                f"[大小姐模式] 未找到管家 handoff 工具: {BUTLER_HANDOFF_TOOL_NAME}，"
                "请确保已在配置中启用 butler subagent"
            )

        # 4. 注入大小姐模式说明
        if MAID_SYSTEM_PROMPT_APPEND not in req.system_prompt:
            req.system_prompt += MAID_SYSTEM_PROMPT_APPEND
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
        # 检查是否是工具调用
        if resp.tools_call_name:
            # 如果主模型尝试直接调用工具（不应该发生），记录警告
            non_butler_tools = [
                name
                for name in resp.tools_call_name
                if name != BUTLER_HANDOFF_TOOL_NAME
            ]
            if non_butler_tools:
                logger.warning(
                    f"[大小姐模式] 主模型尝试直接调用非管家工具: {non_butler_tools}"
                )
            else:
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
