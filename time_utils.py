"""大小姐管家模式插件 - UTC 时间工具函数。"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(UTC)


def iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return utcnow().isoformat()
