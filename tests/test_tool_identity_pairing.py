"""_TurnHooks 同名工具并发时按 tool 对象身份配对结果。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.drivers import _TurnHooks


class _FakeDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def emit_tool_call(self, call_id, name, arguments, _step):
        self.calls.append(("call", {"callId": call_id, "name": name, "arguments": arguments}))

    async def emit_tool_result(self, call_id, name, text, is_error, _step, _tool_args=None):
        self.calls.append(
            ("result", {"callId": call_id, "name": name, "text": text, "isError": is_error})
        )


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _TextPart:
    def __init__(self, text: str):
        self.text = text


class _Result:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [_TextPart(text)]
        self.isError = is_error


def _run(coro):
    return asyncio.run(coro)


class TestToolIdentityPairing:
    def test_same_name_tools_pair_by_identity(self):
        driver = _FakeDriver()
        hooks = _TurnHooks(driver, {"step": 0})
        tool_a = _Tool("web_search")
        tool_b = _Tool("web_search")

        _run(hooks.on_tool_start(None, tool_a, {"q": "a"}))
        _run(hooks.on_tool_start(None, tool_b, {"q": "b"}))
        _run(hooks.on_tool_end(None, tool_b, None, _Result("result-b")))
        _run(hooks.on_tool_end(None, tool_a, None, _Result("result-a")))

        calls = driver.calls
        assert [kind for kind, _ in calls] == ["call", "call", "result", "result"]
        first_call_id = calls[0][1]["callId"]
        second_call_id = calls[1][1]["callId"]
        assert first_call_id != second_call_id
        # b 的结果必须落在 b 的 callId 上，a 的落在 a 的 —— 即使名字相同且 b 先结束
        assert calls[2][1]["callId"] == second_call_id
        assert calls[2][1]["text"] == "result-b"
        assert calls[3][1]["callId"] == first_call_id
        assert calls[3][1]["text"] == "result-a"

    def test_unknown_tool_gets_new_id(self):
        driver = _FakeDriver()
        hooks = _TurnHooks(driver, {"step": 0})
        tool_a = _Tool("web_search")
        _run(hooks.on_tool_start(None, tool_a, {}))
        # end 收到一个从未 start 过的 tool 对象（异常路径）：不能偷走 a 的 callId
        stranger = _Tool("web_search")
        _run(hooks.on_tool_end(None, stranger, None, _Result("x")))
        a_result = next(d for kind, d in driver.calls if kind == "result")
        assert a_result["callId"] not in [d["callId"] for kind, d in driver.calls if kind == "call"]
        _run(hooks.close_unfinished())
        # a 的收尾事件仍然发放
        assert any(kind == "result" and d["text"].startswith("error:") for kind, d in driver.calls)

    def test_emitted_ids_align_with_step_diff(self):
        driver = _FakeDriver()
        hooks = _TurnHooks(driver, {"step": 0})
        hooks.begin_step()
        _run(hooks.on_tool_start(None, _Tool("t1"), {}))
        _run(hooks.on_tool_start(None, _Tool("t2"), {}))
        assert len(hooks.emitted) == 2
        assert c.new_id() != hooks.emitted[0]
