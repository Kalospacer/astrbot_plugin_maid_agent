"""重接后配置的行为测试：主模型工具可见性策略、原话开关、LLM 原始请求日志。

覆盖 2.0 重写时被架空、现已重新实现的四个配置项：
- ``hide_native_tools`` / ``hide_transfer_tools`` → ``apply_main_tool_policy``
- ``include_raw_user_input`` → ``render_dispatch_prompt``
- ``log_raw_llm_io`` → ``harness._log`` 的 DEBUG dump 助手
"""

from __future__ import annotations

from astrbot_plugin_maid_agent.config import render_dispatch_prompt
from astrbot_plugin_maid_agent.constants import CALL_MAID_TOOL_NAME, MAID_TASK_TOOL_NAME
from astrbot_plugin_maid_agent.toolset_adapter import apply_main_tool_policy

from astrbot.core.agent.tool import FunctionTool, ToolSet


def _tool(name: str) -> FunctionTool:
    return FunctionTool(
        name=name,
        description="",
        parameters={"type": "object", "properties": {}},
    )


def _toolset(*names: str) -> ToolSet:
    toolset = ToolSet()
    for name in names:
        toolset.add_tool(_tool(name))
    return toolset


def _names(toolset: ToolSet) -> set[str]:
    return {tool.name for tool in toolset.tools}


class TestApplyMainToolPolicy:
    def test_both_off_unchanged(self):
        toolset = _toolset("send_message_to_user", CALL_MAID_TOOL_NAME, "transfer_to_butler")
        apply_main_tool_policy(
            toolset,
            hide_native_tools=False,
            hide_transfer_tools=False,
        )
        assert _names(toolset) == {
            "send_message_to_user",
            CALL_MAID_TOOL_NAME,
            "transfer_to_butler",
        }

    def test_hide_native_keeps_only_maid_tools(self):
        toolset = _toolset(
            "send_message_to_user",
            CALL_MAID_TOOL_NAME,
            MAID_TASK_TOOL_NAME,
            "transfer_to_butler",
            "web_search",
        )
        apply_main_tool_policy(
            toolset,
            hide_native_tools=True,
            hide_transfer_tools=True,
        )
        assert _names(toolset) == {CALL_MAID_TOOL_NAME, MAID_TASK_TOOL_NAME}

    def test_hide_transfer_only_when_native_off(self):
        toolset = _toolset(
            "send_message_to_user",
            CALL_MAID_TOOL_NAME,
            "transfer_to_butler",
            "transfer_to_researcher",
            "web_search",
        )
        apply_main_tool_policy(
            toolset,
            hide_native_tools=False,
            hide_transfer_tools=True,
        )
        assert _names(toolset) == {"send_message_to_user", CALL_MAID_TOOL_NAME, "web_search"}

    def test_guard_no_call_maid_means_no_change(self):
        # 工具集里没有 call_maid（第三方 runner 自行组装的请求）时绝不动它
        toolset = _toolset("send_message_to_user", "web_search")
        apply_main_tool_policy(
            toolset,
            hide_native_tools=True,
            hide_transfer_tools=False,
        )
        assert _names(toolset) == {"send_message_to_user", "web_search"}

    def test_none_input(self):
        assert (
            apply_main_tool_policy(
                None,
                hide_native_tools=True,
                hide_transfer_tools=False,
            )
            is None
        )


class TestRenderDispatchPrompt:
    def test_include_raw_user_input(self):
        prompt = render_dispatch_prompt(
            "{user_input_block}{maid_request_block}请办妥",
            true_user_input="帮我看看今天天气",
            request_text="查一下今天的天气",
            include_raw_user_input=True,
        )
        assert "【对方原话】" in prompt
        assert "帮我看看今天天气" in prompt
        assert "【大小姐请求】" in prompt
        assert "查一下今天的天气" in prompt
        assert prompt.endswith("请办妥")

    def test_exclude_raw_user_input(self):
        prompt = render_dispatch_prompt(
            "{user_input_block}{maid_request_block}请办妥",
            true_user_input="帮我看看今天天气",
            request_text="查一下今天的天气",
            include_raw_user_input=False,
        )
        assert "对方原话" not in prompt
        assert "帮我看看今天天气" not in prompt
        assert "查一下今天的天气" in prompt

    def test_empty_user_input_never_adds_block(self):
        prompt = render_dispatch_prompt(
            "{user_input_block}{maid_request_block}请办妥",
            true_user_input="   ",
            request_text="x",
            include_raw_user_input=True,
        )
        assert "对方原话" not in prompt

    def test_invalid_template_falls_back_to_default(self):
        prompt = render_dispatch_prompt(
            "{broken",  # 无法解析的模板 → 回退默认
            true_user_input="帮我看看今天天气",
            request_text="查一下今天的天气",
            include_raw_user_input=True,
        )
        assert "【对方原话】" in prompt
        assert "【大小姐请求】" in prompt


class TestRawLlmDump:
    def test_dump_request_and_output(self, monkeypatch):
        from astrbot_plugin_maid_agent.harness import _log as raw_log

        calls: list[tuple] = []

        class _FakeLogger:
            def debug(self, *args, **_kwargs):
                calls.append(args)

        monkeypatch.setattr(raw_log, "logger", _FakeLogger())

        class _Req:
            prompt = "帮我查天气"
            system_prompt = "你是大小姐的管家"
            contexts = [{"role": "user", "content": "帮我看看今天天气"}]
            func_tool = None

        raw_log.dump_raw_llm_request(_Req(), source="maid")
        raw_log.dump_raw_llm_output("今天晴，28 度", source="maid")

        assert len(calls) == 2
        assert "原始请求" in calls[0][0]
        # 参数顺序: fmt, source, prompt, system_prompt, contexts, tools
        assert calls[0][1] == "maid"
        assert calls[0][2] == "帮我查天气"
        assert calls[0][3] == "你是大小姐的管家"
        assert "模型输出" in calls[1][0]
        assert calls[1][1] == "maid"
        assert calls[1][2] == "今天晴，28 度"

    def test_dump_tolerates_missing_attrs(self, monkeypatch):
        from astrbot_plugin_maid_agent.harness import _log as raw_log

        calls: list[tuple] = []

        class _FakeLogger:
            def debug(self, *args, **_kwargs):
                calls.append(args)

        monkeypatch.setattr(raw_log, "logger", _FakeLogger())

        raw_log.dump_raw_llm_request(None, source="main")
        assert len(calls) == 1
        assert "原始请求" in calls[0][0]
