"""
大小姐管家模式插件 - 上下文清洗器

负责清洗主模型的请求上下文，过滤掉 tool role 和 tool_calls 痕迹。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.provider import ProviderRequest


def sanitize_contexts(req: ProviderRequest) -> int:
    """
    清洗 contexts，过滤掉 tool role 和 tool_calls。

    Args:
        req: ProviderRequest 对象

    Returns:
        过滤掉的消息数量
    """
    raw_contexts = req.contexts if isinstance(req.contexts, list) else []
    natural_contexts = []
    for ctx in raw_contexts:
        if not isinstance(ctx, dict):
            continue
        role = ctx.get("role", "")
        # 过滤掉 tool 角色的消息
        if role == "tool":
            continue
        # 过滤掉包含 tool_calls 的 assistant 消息
        if role == "assistant" and "tool_calls" in ctx:
            continue
        natural_contexts.append(ctx)

    removed_count = len(raw_contexts) - len(natural_contexts)
    req.contexts = natural_contexts
    return removed_count
