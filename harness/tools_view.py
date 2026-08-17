"""工具视图呈现器（对应 apiproxy 的 viewFor：事件发射时纯派生，不落盘）。

AstrBot 工具名 → ToolCallView / ToolResultView。词表对齐
工具视图：generic / terminal / read / diff / search。
未注册的工具落到 generic 卡。
"""

from __future__ import annotations

import json
from typing import Any

from . import contracts as c

# 工具名 → (kind, 卡类型)。terminal 类工具走终端卡，read 类走代码卡。
_TOOL_KIND_RULES: list[tuple[str, str, str]] = [
    ("bash", "execute", "terminal"),
    ("shell", "execute", "terminal"),
    ("command", "execute", "terminal"),
    ("cmd", "execute", "terminal"),
    ("terminal", "execute", "terminal"),
    ("run_command", "execute", "terminal"),
    ("read", "read", "read"),
    ("read_file", "read", "read"),
    ("view_file", "read", "read"),
    ("get_file_content", "read", "read"),
    ("write", "edit", "diff"),
    ("edit", "edit", "diff"),
    ("write_file", "edit", "diff"),
    ("edit_file", "edit", "diff"),
    ("apply_patch", "edit", "diff"),
    ("create_file", "edit", "diff"),
    ("grep", "search", "search"),
    ("search", "search", "search"),
    ("glob", "search", "search"),
    ("find", "search", "search"),
    ("web_search", "search", "web"),
    ("search_web", "search", "web"),
    ("web_fetch", "fetch", "web"),
    ("visit_webpage", "fetch", "web"),
    ("remove", "delete", "generic"),
    ("delete", "delete", "generic"),
    ("move", "move", "generic"),
]

_ARG_PATH_KEYS = ("path", "file_path", "file", "filename", "target", "dir", "directory")


def _rule(tool_name: str) -> tuple[str, str] | None:
    lowered = (tool_name or "").lower()
    for needle, kind, card in _TOOL_KIND_RULES:
        if needle in lowered:
            return kind, card
    return None


def _arg_str(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(arguments)


def _arg_dict(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _first_path(args: dict) -> str | None:
    for key in _ARG_PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def present_call(tool_name: str, arguments: Any) -> dict | None:
    """ToolCallView；无法呈现时返回 None（客户端走 generic JSON 卡）。"""
    rule = _rule(tool_name)
    args = _arg_dict(arguments)
    arg_str = _arg_str(arguments)

    if rule is None:
        return c.generic_call_view(f"调用 {tool_name}", "other", raw_input=arg_str)

    kind, card = rule
    path = _first_path(args)

    if card == "terminal":
        command = args.get("command") or arg_str
        return c.terminal_call_view(str(command), cwd=path)
    if card == "diff":
        diffs = []
        old_text = args.get("old_text") or args.get("oldText") or args.get("original")
        new_text = args.get("new_text") or args.get("newText") or args.get("content") or args.get("text")
        if path is not None or new_text is not None:
            diffs.append(
                {
                    "path": path or "(未命名)",
                    "oldText": str(old_text) if old_text is not None else None,
                    "newText": str(new_text or ""),
                }
            )
        return c.diff_call_view(f"{tool_name} {path or ''}".strip(), diffs)
    if card == "read":
        return c.generic_call_view(
            f"读取 {path or tool_name}",
            "read",
            raw_input=arg_str,
            locations=[{"path": path, "line": args.get("offset") or args.get("line")} for _ in [0] if path],
        )
    if card == "search":
        query = args.get("query") or args.get("pattern") or args.get("keyword") or arg_str
        return c.generic_call_view(f"搜索 {query}", "search", raw_input=arg_str)
    if card == "web":
        url_or_query = args.get("url") or args.get("query") or arg_str
        return c.generic_call_view(f"{tool_name} {url_or_query}", kind, raw_input=arg_str)
    return c.generic_call_view(f"{tool_name} {path or ''}".strip(), kind, raw_input=arg_str)


def present_result(tool_name: str, result_text: str, arguments: Any) -> dict | None:
    """ToolResultView；输入是 hooks 捕获的文本化结果。"""
    rule = _rule(tool_name)
    args = _arg_dict(arguments)
    is_error = result_text.lower().startswith("error")

    if rule is None:
        return None

    kind, card = rule
    path = _first_path(args)

    if card == "terminal":
        view = c.terminal_result_view(output=result_text or None)
        if is_error:
            view["exitCode"] = 1
        return view
    if card == "read" and path:
        lines = result_text.splitlines()
        offset = int(args.get("offset") or 1)
        numbered = [
            {"number": offset + i, "text": line}
            for i, line in enumerate(lines)
        ]
        lang = path.rsplit(".", 1)[-1].lower() if "." in path else None
        return c.read_result_view(
            path=path,
            offset=offset,
            lines=numbered,
            total_lines=offset + len(lines) - 1,
            lang=lang,
        )
    if card == "diff":
        new_text = args.get("new_text") or args.get("newText") or args.get("content") or args.get("text")
        diffs = [
            {
                "path": path or "(未命名)",
                "oldText": None,
                "newText": str(new_text or result_text or ""),
            }
        ]
        view: dict = c.diff_call_view(f"{tool_name} 完成", diffs)
        view["card"] = "diff"
        return {"card": "diff", "diffs": diffs}
    # search / web / generic：文本足够，返回 None 让客户端渲染原始内容
    return None
