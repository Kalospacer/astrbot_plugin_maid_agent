"""
大小姐管家模式插件 - 工具管理器

负责管理主模型的工具列表，裁剪为仅保留管家 handoff。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import ToolSet

from .constants import BUTLER_HANDOFF_TOOL_NAME

if TYPE_CHECKING:
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.star.context import Context


def trim_tools_to_butler_handoff(req: ProviderRequest, context: "Context") -> bool:
    """
    裁剪工具列表为仅保留管家 handoff。

    通过插件 context 的 LLM Tool Manager 获取工具，确保遵守管理员的
    激活/禁用设置（tool.active）。若 transfer_to_butler 工具不存在或
    已被禁用，则不修改 req.func_tool，并返回 False。

    Args:
        req:     ProviderRequest 对象
        context: 插件 Star context（self.context），用于获取 LLM Tool Manager

    Returns:
        是否成功找到并注入了处于激活状态的管家 handoff
    """
    tool_manager = context.get_llm_tool_manager()
    butler_handoff = tool_manager.get_func(BUTLER_HANDOFF_TOOL_NAME)

    # 工具不存在，或管理员已将其禁用——不强行注入
    if butler_handoff is None or not butler_handoff.active:
        return False

    req.func_tool = ToolSet()
    req.func_tool.add_tool(butler_handoff)
    return True
