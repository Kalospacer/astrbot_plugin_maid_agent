"""日志门面：AstrBot 运行时用 astrbot logger，离线测试回落 std logging。"""

from __future__ import annotations

import logging

try:  # pragma: no cover - AstrBot 运行时
    from astrbot.api import logger as _logger
except ImportError:
    _logger = logging.getLogger("astrbot_plugin_maid_agent.harness")

logger = _logger


def dump_raw_llm_request(req, *, source: str) -> None:
    """log_raw_llm_io 开启时的完整 LLM 请求 DEBUG dump（可能含敏感信息）。

    Args:
        req: 带 ``prompt`` / ``system_prompt`` / ``contexts`` / ``func_tool``
            属性的 ProviderRequest（或其等价物）。
        source: 日志来源标识，如 ``"main"`` / ``"maid"``。
    """
    func_tool = getattr(req, "func_tool", None)
    tool_names = func_tool.names() if func_tool is not None else []
    logger.debug(
        "[maid][log_raw_llm_io] %s 原始请求 | prompt=%r | system_prompt=%r | contexts=%r | tools=%r",
        source,
        getattr(req, "prompt", ""),
        getattr(req, "system_prompt", ""),
        getattr(req, "contexts", []),
        tool_names,
    )


def dump_raw_llm_output(text: str, *, source: str) -> None:
    """log_raw_llm_io 开启时的模型输出 DEBUG dump（可能含敏感信息）。

    Args:
        text: 模型最终 completion_text。
        source: 日志来源标识，如 ``"main"`` / ``"maid"``。
    """
    logger.debug(
        "[maid][log_raw_llm_io] %s 模型输出 | completion_text=%r",
        source,
        text,
    )
