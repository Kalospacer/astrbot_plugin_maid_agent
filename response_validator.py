"""
大小姐管家模式插件 - 响应观测器

在 XML 协议模式下，仅用于观测主模型是否仍残留原生工具调用倾向。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.provider import LLMResponse


def validate_llm_response(resp: LLMResponse) -> list[str]:
    """
    返回主模型响应中的原生工具调用名称列表。

    在 XML 协议模式下，这仅作为低优先级观测信息，
    不再承担主路径校验职责。
    """
    return list(resp.tools_call_name or [])
