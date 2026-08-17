"""日志门面：AstrBot 运行时用 astrbot logger，离线测试回落 std logging。"""

from __future__ import annotations

import logging

try:  # pragma: no cover - AstrBot 运行时
    from astrbot.api import logger as _logger
except ImportError:  # 测试/离线环境
    _logger = logging.getLogger("astrbot_plugin_maid_agent.harness")

logger = _logger
