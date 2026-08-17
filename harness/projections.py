"""投影注册表（对应 session-projection）。

每个投影是纯函数单元：init/apply/view，按事件顺序折叠，输出整值 + seq 水位。
宿主在事件落盘后把变化值经 session/projection 帧推出；历史尾页带同一形状的基线。
"""

from __future__ import annotations

from .contracts import is_token_delta


class ProjectionUnit:
    key: str = ""

    def init(self) -> dict:  # state
        raise NotImplementedError

    def apply(self, state: dict, event: dict) -> dict:
        raise NotImplementedError

    def view(self, state: dict):
        return state.get("value")


class TitleProjection(ProjectionUnit):
    """title: string | null —— 最新 session/title 事件的值。"""

    key = "title"

    def init(self):
        return {"value": None}

    def apply(self, state, event):
        if event.get("type") == "session/title":
            state["value"] = event.get("data", {}).get("title")
        return state


class SessionStatsProjection(ProjectionUnit):
    """sessionStats: turns/steps/llmMs/toolMs/ttftMs/ttftSteps/decodeMs/decodeTokens。"""

    key = "sessionStats"

    def init(self):
        return {
            "value": {
                "turns": 0,
                "steps": 0,
                "llmMs": 0,
                "toolMs": 0,
                "ttftMs": 0,
                "ttftSteps": 0,
                "decodeMs": 0,
                "decodeTokens": 0,
            },
            "_step_start": None,
            "_tool_calls": {},
            "_ttft_done": False,
        }

    def apply(self, state, event):
        etype = event.get("type")
        data = event.get("data", {})
        value = state["value"]

        if etype == "turn/end":
            value["turns"] += 1
        elif etype == "step/end":
            value["steps"] += 1
        elif etype == "step/start":
            state["_step_start"] = event.get("time")
        elif etype == "assistant/message":
            start = state.pop("_step_start", None)
            if start is not None:
                value["llmMs"] += max(0, int(event.get("time", 0)) - int(start))
            for block in data.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    value["decodeTokens"] += len(block.get("text", ""))
        elif etype == "tool/call":
            state["_tool_calls"][data.get("callId")] = event.get("time")
        elif etype == "tool/result":
            started = state["_tool_calls"].pop(data.get("callId"), None)
            if started is not None:
                value["toolMs"] += max(0, int(event.get("time", 0)) - int(started))
        elif etype == "assistant/chunk" and not state["_ttft_done"]:
            if is_token_delta(data.get("chunk", {})):
                start = state.get("_step_start")
                if start is not None:
                    value["ttftMs"] = max(0, int(event.get("time", 0)) - int(start))
                    value["ttftSteps"] = 1
                    state["_ttft_done"] = True

        value["decodeMs"] = value["llmMs"]
        return state

    def view(self, state):
        return state["value"]


class TokenUsageProjection(ProjectionUnit):
    """tokenUsage: 不相交桶合计（uncachedInput/output/cacheRead/cacheWrite）。"""

    key = "tokenUsage"

    def init(self):
        return {
            "value": {
                "uncachedInputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
            }
        }

    def apply(self, state, event):
        if event.get("type") == "assistant/message":
            usage = event.get("data", {}).get("usage")
            if isinstance(usage, dict):
                value = state["value"]
                value["uncachedInputTokens"] += int(usage.get("inputTokens", 0) or 0)
                value["outputTokens"] += int(usage.get("outputTokens", 0) or 0)
                value["cacheReadTokens"] += int(usage.get("cacheReadTokens", 0) or 0)
                value["cacheWriteTokens"] += int(usage.get("cacheWriteTokens", 0) or 0)
        return state


DEFAULT_PROJECTIONS: list[ProjectionUnit] = [
    TitleProjection(),
    SessionStatsProjection(),
    TokenUsageProjection(),
]


class ProjectionRegistry:
    """按 (session_id, last_seq) 缓存的投影计算器。"""

    def __init__(self, units: list[ProjectionUnit] | None = None):
        self.units = units if units is not None else list(DEFAULT_PROJECTIONS)

    def compute(self, session_id: str, events: list[dict]) -> dict:
        """全量折叠出 {asOfSeq, values}。events 为该会话全部事件。"""
        as_of = len(events) - 1
        values: dict = {}
        for unit in self.units:
            state = unit.init()
            for event in events:
                state = unit.apply(state, event)
            values[unit.key] = unit.view(state)
        return {"asOfSeq": as_of, "values": values}

    def compute_keys(self, session_id: str, events: list[dict], keys: set[str]) -> dict:
        as_of = len(events) - 1
        values: dict = {}
        for unit in self.units:
            if unit.key not in keys:
                continue
            state = unit.init()
            for event in events:
                state = unit.apply(state, event)
            values[unit.key] = unit.view(state)
        return {"asOfSeq": as_of, "values": values}
