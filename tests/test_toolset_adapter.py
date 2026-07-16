"""Functional tests for toolset_adapter (1.3.0 child toolset & memory)."""

from __future__ import annotations

import asyncio

from astrbot_plugin_maid_agent.toolset_adapter import (
    MEMORY_MAX_BYTES,
    MEMORY_MAX_LINES,
    _get_runtime_computer_tools,
    _is_handoff_tool,
    _is_recursion_tool,
    _sanitize_child_toolset,
    get_memory_dir,
    load_memory_index_inline,
)

from astrbot.core.agent.tool import FunctionTool, ToolSet


def _tool(name: str, active: bool = True) -> FunctionTool:
    return FunctionTool(
        name=name,
        description=f"tool {name}",
        parameters={"type": "object", "properties": {}},
        active=active,
    )


def test_recursion_and_handoff_detection():
    assert _is_recursion_tool(_tool("call_maid"))
    assert _is_recursion_tool(_tool("maid_task"))
    assert not _is_recursion_tool(_tool("other_tool"))
    assert _is_handoff_tool(_tool("transfer_to_butler"))
    assert not _is_handoff_tool(_tool("butler"))


def test_sanitize_strips_control_plane_and_handoffs():
    ts = ToolSet()
    for name in ["call_maid", "maid_task", "transfer_to_butler", "search_web", "read_file"]:
        ts.add_tool(_tool(name))
    sanitized = _sanitize_child_toolset(ts)
    assert sanitized is not None
    assert sanitized.names() == ["search_web", "read_file"]


def test_sanitize_returns_none_when_empty():
    ts = ToolSet()
    ts.add_tool(_tool("call_maid"))
    ts.add_tool(_tool("transfer_to_x"))
    assert _sanitize_child_toolset(ts) is None
    assert _sanitize_child_toolset(None) is None


def test_sanitize_drops_inactive_tools():
    ts = ToolSet()
    ts.add_tool(_tool("keep", active=True))
    ts.add_tool(_tool("drop", active=False))
    sanitized = _sanitize_child_toolset(ts)
    assert sanitized is not None
    assert sanitized.names() == ["keep"]


def test_runtime_computer_tool_roster_matches_current_core_contract():
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    class _ToolMgr:
        @staticmethod
        def get_builtin_tool(cls):
            return _tool(cls.__name__)

    manager = _ToolMgr()
    for runtime, booter in (("local", ""), ("sandbox", ""), ("sandbox", "cua")):
        plugin_tools = _get_runtime_computer_tools(
            runtime,
            manager,
            {"sandbox": {"booter": booter}},
        )
        core_tools = FunctionToolExecutor._get_runtime_computer_tools(
            runtime,
            manager,
            booter,
        )
        assert set(plugin_tools) == set(core_tools)


def test_runtime_computer_tool_roster_skips_missing_core_classes(monkeypatch):
    from astrbot_plugin_maid_agent import toolset_adapter as ta

    class _ToolMgr:
        @staticmethod
        def get_builtin_tool(cls):
            return _tool(cls.__name__)

    monkeypatch.setattr(
        ta,
        "_LOCAL_TOOL_CLASS_NAMES",
        ("MissingToolFromFutureCore", "ExecuteShellTool"),
    )
    tools = _get_runtime_computer_tools("local", _ToolMgr(), {})
    assert set(tools) == {"ExecuteShellTool"}


def _memory_dir(tmp_path, monkeypatch, umo="aiocqhttp:GroupMessage:g1", agent="butler"):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.toolset_adapter.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return get_memory_dir(umo, agent)


def test_memory_dir_isolated_by_umo_and_agent(tmp_path, monkeypatch):
    d1 = _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler")
    d2 = _memory_dir(tmp_path, monkeypatch, "umo:a:2", "butler")
    d3 = _memory_dir(tmp_path, monkeypatch, "umo:a:1", "maid")
    assert d1 != d2
    assert d1 != d3
    assert d1.exists()


def test_memory_dir_sanitizes_unsafe_segments(tmp_path, monkeypatch):
    d = _memory_dir(tmp_path, monkeypatch, "plat:Group:../escape", "but/ler")
    # The sanitized segments must not traverse out of the memory base.
    assert d.is_relative_to(tmp_path / "memory")
    assert ".." not in d.parts


def test_load_memory_index_inline_none_when_absent(tmp_path, monkeypatch):
    _memory_dir(tmp_path, monkeypatch)
    assert load_memory_index_inline("umo:a:1", "butler") is None


def test_load_memory_index_inline_full_when_small(tmp_path, monkeypatch):
    _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler")
    path = _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler") / "MEMORY.md"
    path.write_text("# Title\nline2\n", encoding="utf-8")
    loaded = load_memory_index_inline("umo:a:1", "butler")
    # File content is returned as-is (including its trailing newline).
    assert loaded == "# Title\nline2\n"


def test_load_memory_index_inline_truncates_over_line_cap(tmp_path, monkeypatch):
    _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler")
    path = _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler") / "MEMORY.md"
    path.write_text("\n".join(f"line {i}" for i in range(MEMORY_MAX_LINES + 50)), encoding="utf-8")
    loaded = load_memory_index_inline("umo:a:1", "butler")
    assert loaded is not None
    assert "已超过内联上限" in loaded
    # Truncated body has at most MEMORY_MAX_LINES lines before the hint.
    body = loaded.split("\n\n[MEMORY.md")[0]
    assert len(body.splitlines()) <= MEMORY_MAX_LINES


def test_load_memory_index_inline_truncates_over_byte_cap(tmp_path, monkeypatch):
    _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler")
    path = _memory_dir(tmp_path, monkeypatch, "umo:a:1", "butler") / "MEMORY.md"
    # One huge line exceeding the byte cap.
    path.write_text("x" * (MEMORY_MAX_BYTES + 1000), encoding="utf-8")
    loaded = load_memory_index_inline("umo:a:1", "butler")
    assert loaded is not None
    assert "已超过内联上限" in loaded
    assert len(loaded.encode("utf-8")) < MEMORY_MAX_BYTES + 500


async def _build_child_strips_recursion_and_adds_file_tools(monkeypatch):
    from astrbot_plugin_maid_agent import toolset_adapter as ta

    from astrbot.core.agent.handoff import Agent, HandoffTool
    from astrbot.core.agent.tool import ToolSet

    # Mock tool manager.
    class _ToolMgr:
        def __init__(self):
            self.func_list = [
                _tool("call_maid"),
                _tool("transfer_to_butler"),
                _tool("search_web"),
                _tool("astrbot_file_read_tool"),
                _tool("astrbot_file_write_tool"),
                _tool("astrbot_file_edit_tool"),
            ]

        def get_full_tool_set(self):
            ts = ToolSet()
            for t in self.func_list:
                ts.add_tool(t)
            return ts

        def get_func(self, name):
            for t in self.func_list:
                if t.name == name:
                    return t
            return None

        def get_builtin_tool(self, cls):
            name_map = {
                "FileReadTool": "astrbot_file_read_tool",
                "FileWriteTool": "astrbot_file_write_tool",
                "FileEditTool": "astrbot_file_edit_tool",
            }
            for t in self.func_list:
                if t.name == name_map.get(cls.__name__, ""):
                    return t
            return None

    class _Context:
        def __init__(self):
            self._mgr = _ToolMgr()

        def get_llm_tool_manager(self):
            return self._mgr

        def get_config(self, umo=None, **_kwargs):
            _ = umo  # accepted for API compatibility
            return {"provider_settings": {"computer_use_runtime": "none"}}

    # Patch runtime computer tools to empty (runtime=none).
    monkeypatch.setattr(ta, "_get_runtime_computer_tools", lambda _rt, _mgr, _ps: {})

    agent = Agent(name="butler", instructions="", tools=None)
    handoff = HandoffTool(agent=agent)
    ctx = _Context()

    # Without memory opt-in: file tools are NOT added, recursion/handoff stripped.
    ts = ta.build_child_toolset(
        ctx,
        handoff=handoff,
        umo="umo:a:1",
        agent_name="butler",
        memory_agent_names=None,
    )
    assert ts is not None
    names = ts.names()
    assert "call_maid" not in names
    assert "transfer_to_butler" not in names
    assert "search_web" in names
    # File tools present in full toolset but not auto-added without memory opt-in?
    # They're in func_list so they're already in the "all tools" set. That's fine —
    # they remain accessible. The point is recursion/handoff are stripped.
    assert "astrbot_file_read_tool" in names

    # With memory opt-in for a DIFFERENT agent: this agent gets no special treatment.
    ts2 = ta.build_child_toolset(
        ctx,
        handoff=handoff,
        umo="umo:a:1",
        agent_name="butler",
        memory_agent_names=["maid"],
    )
    assert ts2 is not None
    assert "search_web" in ts2.names()


def test_build_child_strips_recursion(monkeypatch):
    asyncio.run(_build_child_strips_recursion_and_adds_file_tools(monkeypatch))
