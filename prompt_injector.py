"""
大小姐管家模式插件 - 提示注入器

负责为主模型注入大小姐角色说明。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.provider import ProviderRequest


def build_maid_system_prompt_append(call_tag_name: str, default_agent_name: str) -> str:
    return (
        f"\n- 当你需要呼叫管家帮忙完成任务时，请在回复末尾附加 XML 块咒语："
        f'<{call_tag_name} agent="{default_agent_name}">这里写给管家的要求</{call_tag_name}>'
        "\n- 如果不需要呼叫管家帮忙，就不要说这个咒语"
        "\n- XML 标签中的内容是你对管家的任务要求\n"
    )


def inject_maid_system_prompt(
    req: ProviderRequest,
    call_tag_name: str,
    default_agent_name: str,
) -> bool:
    """
    注入大小姐模式说明到系统提示。

    Args:
        req: ProviderRequest 对象

    Returns:
        是否成功注入（如果已存在则返回 False）
    """
    prompt_append = build_maid_system_prompt_append(call_tag_name, default_agent_name)
    if prompt_append not in req.system_prompt:
        req.system_prompt += prompt_append
        return True
    return False
