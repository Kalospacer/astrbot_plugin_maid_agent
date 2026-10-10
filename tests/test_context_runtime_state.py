"""任务与上下文独立、空闲窗口及无损消息快照的针对性测试。"""


import pytest
from astrbot_plugin_maid_agent.config import ConfigValidationError, load_maid_mode_config
from astrbot_plugin_maid_agent.harness.chat_state import ChatStateStore
from astrbot_plugin_maid_agent.harness.context_state import ContextState, main_block
from astrbot_plugin_maid_agent.harness.tasks import TaskStore


def test_plugin_execution_budget_and_timeout_config():
    cfg = load_maid_mode_config({})
    assert cfg.max_agent_steps == 128
    assert cfg.session_idle_timeout_hours == 5
    assert load_maid_mode_config({"max_agent_steps": 96, "session_idle_timeout_hours": 0.5}).max_agent_steps == 96
    for value in (0, -1, True, "128"):
        with pytest.raises(ConfigValidationError):
            load_maid_mode_config({"max_agent_steps": value})
    for value in (0, -1, True, float("inf"), "5"):
        with pytest.raises(ConfigValidationError):
            load_maid_mode_config({"session_idle_timeout_hours": value})


def test_idle_scope_expiry_keeps_task_identity(tmp_path):
    clock = [1000]
    tasks = TaskStore(tmp_path)
    chats = ChatStateStore(tmp_path, tasks, clock=lambda: clock[0])
    state = chats.ensure("chat", "conversation")
    task = tasks.create(scope=chats.key("chat"), epoch=state["epoch"], branch="branch", request="查数据库")
    state["branches"]["branch"] = "old-session"
    state["defaultBranch"] = "branch"
    chats.save(state)
    clock[0] += 3_600_001
    assert chats.touch_tool(state, 1) is True
    assert not state["branches"]
    assert state["defaultBranch"] is None
    assert chats.target(state, task["taskId"])["request"] == "查数据库"
    assert chats.get("chat")["contextGeneration"] == 1


def test_running_tasks_prevent_idle_and_last_end_starts_clock(tmp_path):
    clock = [1000]
    tasks = TaskStore(tmp_path)
    chats = ChatStateStore(tmp_path, tasks, clock=lambda: clock[0])
    state = chats.ensure("chat", "cid")
    a = tasks.create(scope=chats.key("chat"), epoch=state["epoch"], branch="a", request="数据库")
    b = tasks.create(scope=chats.key("chat"), epoch=state["epoch"], branch="b", request="网络")
    ar = tasks.start_round(a["taskId"], "sa", "数据库")
    br = tasks.start_round(b["taskId"], "sb", "网络")
    chats.begin(state)
    clock[0] += 10_000_000
    assert chats.touch_tool(state, 0.1) is False
    assert state["idleSince"] is None
    tasks.finish(a["taskId"], ar["roundId"], {"status": "completed", "result": "A"})
    chats.settle(state)
    assert state["idleSince"] is None
    tasks.finish(b["taskId"], br["roundId"], {"status": "completed", "result": "B"})
    chats.settle(state)
    assert state["idleSince"] == clock[0]
    clock[0] += 100
    chats.touch_tool(state, 0.1)
    assert state["idleSince"] == clock[0]


def test_results_and_delivery_are_per_task_round(tmp_path):
    tasks = TaskStore(tmp_path)
    a = tasks.create(scope="chat", epoch="epoch", branch="branch", request="A")
    first = tasks.start_round(a["taskId"], "same-session", "A")
    tasks.finish(a["taskId"], first["roundId"], {"status": "completed", "result": "first"})
    second = tasks.start_round(a["taskId"], "same-session", "继续A")
    tasks.finish(a["taskId"], second["roundId"], {"status": "completed", "result": "second"})
    b = tasks.create(scope="chat", epoch="epoch", branch="branch", request="B")
    tasks.start_round(b["taskId"], "same-session", "B")
    stored = tasks.get(a["taskId"])
    assert [item["result"] for item in stored["rounds"]] == ["first", "second"]
    assert tasks.claim_delivery(a["taskId"], first["roundId"])
    assert not tasks.claim_delivery(a["taskId"], first["roundId"])
    assert tasks.claim_delivery(a["taskId"], second["roundId"])
    assert TaskStore.card(a)["description"] == "A"


def test_context_append_preserves_actual_prefix_and_tool_ids(tmp_path):
    context = ContextState(tmp_path)
    main = [{"role": "user", "content": "检查数据库"}]
    initial = context.prepare(main)
    work = [
        {"role": "user", "content": "派发要求"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "original-id", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "content": "配置内容", "tool_call_id": "original-id"},
        {"role": "assistant", "content": [{"type": "think", "think": "分析", "encrypted": "signature"}, {"type": "text", "text": "结论"}]},
    ]
    context.capture([{"role": "system", "content": "管家"}, *initial, *work], "管家")
    next_context = context.prepare([*main, {"role": "user", "content": "再检查网络"}])
    assert next_context[:len(initial) + len(work)] == [*initial, *work]
    assert "再检查网络" in str(next_context[-1])
    assert context.prepare([*main, {"role": "user", "content": "再检查网络"}]) == next_context
    assert next_context[2]["tool_calls"][0]["id"] == "original-id"


def test_main_compression_rebases_background_not_work(tmp_path):
    context = ContextState(tmp_path)
    initial = context.prepare([{"role": "user", "content": "旧背景"}])
    work = {"role": "assistant", "content": "子代理工作结果"}
    context.capture([*initial, work], "")
    new_main = [{"role": "user", "content": "压缩后的主背景"}]
    assert context.prepare(new_main) == [main_block(new_main), work]


def test_media_snapshot_is_stable_after_source_removed(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"image-content")
    context = ContextState(tmp_path / "session")
    main = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(path)}}]}]
    records = context.prepare(main)
    assert "data:image/png;base64," in str(records)
    # 快照已经独立，不需要再次读取源临时文件。
    assert context.load()["messages"] == records


def test_pending_followup_and_recovery_do_not_repeat_execution(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create(scope="chat", epoch="epoch", branch="b", request="检查")
    tasks.start_round(task["taskId"], "session", "检查")
    item = tasks.add_followup(task["taskId"], "先不要修改", [])
    assert tasks.pending(task["taskId"])[0]["id"] == item["id"]
    tasks.set_followup_status(task["taskId"], [item["id"]], "consumed")
    assert tasks.pending(task["taskId"]) == []
    tasks.interrupt_active()
    assert tasks.get(task["taskId"])["status"] == "interrupted"


def test_fork_copies_completed_snapshot_not_running_tail(tmp_path):
    parent = ContextState(tmp_path / "parent")
    completed = [{"role": "user", "content": "任务"}, {"role": "assistant", "content": "完成第一轮"}]
    parent.capture(completed, "")
    parent.checkpoint(1)
    parent.capture([*completed, {"role": "user", "content": "尚在运行的第二轮"}], "")
    child = ContextState(tmp_path / "child")
    child.seed(parent.fork_state(1))
    assert child.prepare(None) == completed
    with pytest.raises(ValueError, match="没有新格式"):
        parent.fork_state(2)


def test_rebase_keeps_multimedia_background_identification(tmp_path):
    context = ContextState(tmp_path)
    main = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    initial = context.prepare(main)
    from astrbot.core.agent.message import Message

    context.capture([Message.model_validate(item) for item in initial], "")
    next_context = context.prepare([{"role": "user", "content": "新的压缩背景"}])
    assert len(next_context) == 1
    assert "data:image" not in str(next_context)


def test_background_is_reanchored_after_child_context_compression(tmp_path):
    context = ContextState(tmp_path)
    main = [{"role": "system", "content": "主代理人格"}, {"role": "user", "content": "原始背景"}]
    initial = context.prepare(main)
    work = {"role": "assistant", "content": "子代理工作记录"}
    context.capture([*initial, work], "子代理人格")
    context.capture([work], "子代理人格")
    assert context.load()["backgroundNeedsRebase"]
    restarted = ContextState(tmp_path)
    restored = restarted.prepare(main)
    assert restored == [main_block(main[1:]), work]
    assert not restarted.load()["backgroundNeedsRebase"]
    assert "主代理人格" not in str(restored)
    delta = {"role": "user", "content": "新增任务背景"}
    assert restarted.prepare([*main, delta]) == [*restored, main_block([delta])]


def test_reanchor_replaces_remaining_partial_background_once(tmp_path):
    context = ContextState(tmp_path)
    main = [{"role": "user", "content": "背景一"}]
    first = context.prepare(main)
    main.append({"role": "user", "content": "背景二"})
    second = context.prepare(main)
    work = {"role": "assistant", "content": "工作摘要"}
    context.capture([second[-1], work], "")
    assert context.prepare(main) == [main_block(main), work]
    assert context.prepare(main) == [main_block(main), work]
    assert first[0] != second[-1]


def test_restart_keeps_idle_clock_and_default_choice(tmp_path):
    tasks = TaskStore(tmp_path)
    chats = ChatStateStore(tmp_path, tasks, clock=lambda: 1000)
    state = chats.ensure("chat", "cid")
    state["branches"] = {"branch": "session"}
    state["defaultBranch"] = "branch"
    chats.save(state)
    recovered = ChatStateStore(tmp_path, TaskStore(tmp_path), clock=lambda: 2000)
    recovered.recover()
    assert recovered.get("chat")["idleSince"] == 1000
    assert recovered.get("chat")["defaultBranch"] == "branch"
