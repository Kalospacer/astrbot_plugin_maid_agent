"""大小姐管家模式插件 - UTC 时间工具函数。"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()
