"""
大小姐管家模式插件 - 回灌提示构造器
"""

from __future__ import annotations


def build_maid_rephrase_prompt(
    original_user_input: str,
    maid_visible_text: str,
    agent_result: str,
) -> str:
    """构造回灌给大小姐的自然语言提示。"""
    sections: list[str] = []

    if original_user_input.strip():
        sections.append(f"【用户原始输入】\n{original_user_input.strip()}")

    if maid_visible_text.strip():
        sections.append(f"【你刚才准备回复用户的思路】\n{maid_visible_text.strip()}")

    sections.append(f"【与当前问题相关的补充结果】\n{agent_result.strip() or '暂无补充结果。'}")
    sections.append("请你以大小姐身份，结合以上信息自然地回复用户。")
    return "\n\n".join(sections)
