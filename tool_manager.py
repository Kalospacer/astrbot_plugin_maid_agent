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


def trim_tools_to_butler_handoff(req: ProviderRequest) -> bool:
    """
    裁剪工具列表为仅保留管家 handoff。

    Args:
        req: ProviderRequest 对象

    Returns:
        是否成功找到并设置了管家 handoff
    """
    # 延迟导入以避免循环依赖
    from astrbot.core.provider.register import llm_tools

    butler_handoff = llm_tools.get_func(BUTLER_HANDOFF_TOOL_NAME)
    if butler_handoff:
        req.func_tool = ToolSet()
        req.func_tool.add_tool(butler_handoff)
        return True
    return False
