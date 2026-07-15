"""Shared JSON file/string helpers for maid agent persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from astrbot.api import logger


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace so readers never see partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """Load a JSON object from disk; missing/corrupt/non-dict content yields None."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[大小姐模式] 读取 JSON 失败，已跳过: path=%s err=%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def dump_json(data: Any, *, indent: int | None = None) -> str:
    """Serialize to a JSON string for logs or SQLite columns.

    Pretty (indent set) failure path returns ``repr(data)`` for log friendliness.
    Compact failure path returns a JSON object with a ``repr`` field so SQLite
    columns stay parseable.
    """
    try:
        return json.dumps(
            data if data is not None else {},
            ensure_ascii=False,
            indent=indent,
            default=str,
        )
    except Exception:
        if indent is not None:
            return repr(data)
        return json.dumps({"repr": repr(data)}, ensure_ascii=False)


def load_json_value(raw: str | None) -> Any:
    """Parse a JSON string from storage; empty/invalid input yields a safe default."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}
