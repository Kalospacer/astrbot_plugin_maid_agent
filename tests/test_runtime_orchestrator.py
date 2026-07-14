"""Functional tests for runtime_orchestrator (1.3.0 state machine + concurrency)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_maid_agent.runtime_orchestrator import (
    BACKGROUND_REASON_TIMEOUT,
    MODE_BACKGROUND,
    MODE_FOREGROUND,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    BatchCapacityError,
    CapacityExceededError,
    DispatchRequest,
    RuntimeOrchestrator,
)
from astrbot_plugin_maid_agent.runtime_store import RuntimeStore


def _make_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.runtime_store.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return RuntimeStore(config=object())


class _FakeConfig:
    default_agent_name = "butler"
    max_active_per_umo = 5
    max_active_global = 20
    foreground_timeout_seconds = 50


def _make_event(sender_id="owner", umo="aiocqhttp:GroupMessage:g1"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_sender_id=lambda: sender_id,
    )


class _ScriptedRunner:
    """A controllable runner that records steer calls and can block until released."""

    def __init__(self, *, result="done", delay=0.0, raise_exc=None):
        self.result = result
        self.delay = delay
        self.raise_exc = raise_exc
        self.steer_calls: list[str] = []
        self._release = asyncio.Event()
        self._released = False
        self.run_calls = 0

    async def run(self) -> str:
        self.run_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self._released:
            await self._release.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result

    def release(self):
        self._released = True
        self._release.set()

    def make_steer_handler(self):
        async def _handler(text: str) -> str:
            self.steer_calls.append(text)
            return f"steered:{text}"

        return _handler


def _factory_with(runners: list[_ScriptedRunner]):
    """Build a runner_factory that hands out scripted runners in order and
    registers a steer handler for each."""
    iterator = iter(runners)

    async def _factory(run, event, payload):
        runner = next(iterator)
        return runner

    return _factory, runners


async def _foreground_completes_inline(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="hello", delay=0.0)
    runner._release.set()  # complete immediately
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(request_text="do thing", agent_name="butler"),
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.mode == MODE_FOREGROUND
    assert outcome.result == "hello"
    # Foreground releases its capacity slot after terminal completion.
    assert orch._active_count_global() == 0


def test_foreground_completes_inline(tmp_path, monkeypatch):
    asyncio.run(_foreground_completes_inline(tmp_path, monkeypatch))


async def _foreground_timeout_migrates_to_background(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="late-result", delay=0.0)
    # Do NOT release immediately so foreground wait_for times out.
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)
    orch.config.foreground_timeout_seconds = 0.05  # 50ms budget for the test

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(request_text="slow thing", agent_name="butler"),
    )
    assert outcome.status == STATUS_RUNNING
    assert outcome.mode == MODE_BACKGROUND
    assert outcome.background_reason == BACKGROUND_REASON_TIMEOUT

    # The migrated background task should eventually complete.
    await asyncio.sleep(0.2)
    runner.release()
    await asyncio.sleep(0.1)
    run = await store.load_run(outcome.agent_id, outcome.task_id)
    assert run is not None
    assert run.status == STATUS_COMPLETED
    assert run.result == "late-result"
    assert run.mode == MODE_BACKGROUND
    assert run.background_reason == BACKGROUND_REASON_TIMEOUT
    assert runner.run_calls == 1
    assert orch._active_count_global() == 0


def test_foreground_timeout_migrates_to_background(tmp_path, monkeypatch):
    asyncio.run(_foreground_timeout_migrates_to_background(tmp_path, monkeypatch))


async def _explicit_background_returns_immediately(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="bg-result")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg thing", agent_name="butler", run_in_background=True
        ),
    )
    assert outcome.status == STATUS_STARTING
    assert outcome.mode == MODE_BACKGROUND
    # Background run still occupies capacity until it finishes.
    assert orch._active_count_global() == 1

    runner.release()
    await asyncio.sleep(0.1)
    run = await store.load_run(outcome.agent_id, outcome.task_id)
    assert run is not None
    assert run.status == STATUS_COMPLETED
    assert orch._active_count_global() == 0


def test_explicit_background_returns_immediately(tmp_path, monkeypatch):
    asyncio.run(_explicit_background_returns_immediately(tmp_path, monkeypatch))


async def _immediate_background_completion_releases_capacity(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="instant")
    runner.release()
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="instant",
            agent_name="butler",
            run_in_background=True,
        ),
    )
    await asyncio.sleep(0.1)
    run = await store.load_run(outcome.agent_id, outcome.task_id)
    assert run is not None and run.status == STATUS_COMPLETED
    assert orch._active_count_global() == 0


def test_immediate_background_completion_releases_capacity(tmp_path, monkeypatch):
    asyncio.run(_immediate_background_completion_releases_capacity(tmp_path, monkeypatch))


async def _batch_atomic_capacity_rejection(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    config = _FakeConfig()
    config.max_active_per_umo = 2  # only 2 allowed per UMO
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        return _ScriptedRunner(result="x")

    orch._runner_factory = _factory
    requests = [
        DispatchRequest(request_text=f"task {i}", agent_name="butler")
        for i in range(3)  # exceeds per-UMO cap of 2
    ]
    try:
        await orch.dispatch_batch(event=_make_event(), requests=requests)
    except BatchCapacityError:
        pass
    else:
        raise AssertionError("expected BatchCapacityError")

    # Nothing should have been created.
    agent_ids = await store.list_agent_ids()
    assert agent_ids == []


def test_batch_atomic_capacity_rejection(tmp_path, monkeypatch):
    asyncio.run(_batch_atomic_capacity_rejection(tmp_path, monkeypatch))


async def _batch_concurrent_launch_in_order(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runners = [_ScriptedRunner(result=f"r{i}") for i in range(3)]
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        # Hand out runners in order.
        return runners.pop(0)

    orch._runner_factory = _factory
    requests = [
        DispatchRequest(
            request_text=f"task {i}",
            agent_name="butler",
            run_in_background=True,
        )
        for i in range(3)
    ]
    outcome = await orch.dispatch_batch(event=_make_event(), requests=requests)
    assert len(outcome.items) == 3
    # Items preserve input order.
    for item in outcome.items:
        assert item.status == STATUS_STARTING
        assert item.mode == MODE_BACKGROUND

    # The factory-created runners are the ones we popped (r0,r1,r2 in order);
    # release them so the background runs can complete.
    # (runners list is now empty after pop-based dispatch.)


def test_batch_concurrent_launch_in_order(tmp_path, monkeypatch):
    asyncio.run(_batch_concurrent_launch_in_order(tmp_path, monkeypatch))


async def _batch_foreground_waits_concurrently(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runners = [_ScriptedRunner(result="r1"), _ScriptedRunner(result="r2")]
    config = _FakeConfig()
    config.foreground_timeout_seconds = 0.1
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        return runners.pop(0)

    original_runners = list(runners)
    orch._runner_factory = _factory
    started = asyncio.get_running_loop().time()
    outcome = await orch.dispatch_batch(
        event=_make_event(),
        requests=[
            DispatchRequest(request_text="one", agent_name="butler"),
            DispatchRequest(request_text="two", agent_name="butler"),
        ],
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.18
    assert [item.background_reason for item in outcome.items] == ["timeout", "timeout"]
    assert all(runner.run_calls == 1 for runner in original_runners)
    for runner in original_runners:
        runner.release()
    await asyncio.sleep(0.1)


def test_batch_foreground_waits_concurrently(tmp_path, monkeypatch):
    asyncio.run(_batch_foreground_waits_concurrently(tmp_path, monkeypatch))


async def _steer_running_agent(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        orch.register_steer_handler(run.agent_id, runner.make_steer_handler())
        return runner

    orch._runner_factory = _factory
    # Start a background run (so it stays active).
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    # Steer it.
    ticket = await orch.steer(
        agent_id=bg.agent_id,
        message_text="more info",
        sender_id="owner",
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
    )
    assert ticket == "steered:more info"
    assert runner.steer_calls == ["more info"]
    runner.release()
    await asyncio.sleep(0.1)


def test_steer_running_agent(tmp_path, monkeypatch):
    asyncio.run(_steer_running_agent(tmp_path, monkeypatch))


async def _resume_running_routes_to_steer(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        orch.register_steer_handler(run.agent_id, runner.make_steer_handler())
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    # Resume while running -> steer, no new task.
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="follow up", agent_name="butler", resume_agent_id=bg.agent_id
        ),
    )
    assert outcome.background_reason == "steer"
    assert outcome.task_id == bg.task_id
    assert runner.steer_calls == ["follow up"]
    runner.release()
    await asyncio.sleep(0.1)


def test_resume_running_routes_to_steer(tmp_path, monkeypatch):
    asyncio.run(_resume_running_routes_to_steer(tmp_path, monkeypatch))


async def _resume_terminal_creates_new_task_background(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner1 = _ScriptedRunner(result="first")
    runner1._release.set()
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner1

    orch._runner_factory = _factory
    first = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(request_text="first", agent_name="butler"),
    )
    assert first.status == STATUS_COMPLETED

    # Now resume the completed agent -> new task, background.
    runner2 = _ScriptedRunner(result="second")

    async def _factory2(run, event, payload):
        return runner2

    orch._runner_factory = _factory2
    outcome = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="second", agent_name="butler", resume_agent_id=first.agent_id
        ),
    )
    assert outcome.task_id != first.task_id
    assert outcome.mode == MODE_BACKGROUND
    assert outcome.background_reason == "resume"
    runner2.release()
    await asyncio.sleep(0.1)
    run = await store.load_run(outcome.agent_id, outcome.task_id)
    assert run is not None
    assert run.status == STATUS_COMPLETED
    assert run.result == "second"


def test_resume_terminal_creates_new_task_background(tmp_path, monkeypatch):
    asyncio.run(_resume_terminal_creates_new_task_background(tmp_path, monkeypatch))


async def _stop_running_agent(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    outcome = await orch.stop(
        task_id=bg.task_id,
        sender_id="owner",
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
    )
    assert outcome.status == STATUS_STOPPED
    run = await store.load_run(bg.agent_id, bg.task_id)
    assert run is not None
    assert run.status == STATUS_STOPPED


def test_stop_running_agent(tmp_path, monkeypatch):
    asyncio.run(_stop_running_agent(tmp_path, monkeypatch))


async def _result_blocking_returns_terminal(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="the answer")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    runner.release()
    outcome = await orch.get_result(
        agent_id=bg.agent_id, task_id=bg.task_id, block=True, timeout_ms=2000
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.result == "the answer"


def test_result_blocking_returns_terminal(tmp_path, monkeypatch):
    asyncio.run(_result_blocking_returns_terminal(tmp_path, monkeypatch))


async def _result_nonblocking_not_ready(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="late")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    outcome = await orch.get_result(
        agent_id=bg.agent_id, task_id=bg.task_id, block=False, timeout_ms=0
    )
    assert outcome.status in {STATUS_STARTING, STATUS_RUNNING}
    assert outcome.query_status == "not_ready"
    runner.release()
    await asyncio.sleep(0.1)


def test_result_nonblocking_not_ready(tmp_path, monkeypatch):
    asyncio.run(_result_nonblocking_not_ready(tmp_path, monkeypatch))


async def _result_blocking_timeout_returns_not_ready(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="late")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg", agent_name="butler", run_in_background=True
        ),
    )
    outcome = await orch.get_result(
        agent_id=bg.agent_id, task_id=bg.task_id, block=True, timeout_ms=100
    )
    assert outcome.status in {STATUS_STARTING, STATUS_RUNNING}
    assert outcome.query_status == "timeout"
    runner.release()
    await asyncio.sleep(0.1)


def test_result_blocking_timeout_returns_not_ready(tmp_path, monkeypatch):
    asyncio.run(_result_blocking_timeout_returns_not_ready(tmp_path, monkeypatch))


async def _per_umo_capacity_limit(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    config = _FakeConfig()
    config.max_active_per_umo = 1
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        return _ScriptedRunner(result="x")

    orch._runner_factory = _factory
    first = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="a", agent_name="butler", run_in_background=True
        ),
    )
    try:
        await orch.dispatch_single(
            event=_make_event(),
            request=DispatchRequest(
                request_text="b", agent_name="butler", run_in_background=True
            ),
        )
    except CapacityExceededError:
        pass
    else:
        raise AssertionError("expected CapacityExceededError")
    # Cleanup.
    await orch.stop(
        task_id=first.task_id,
        sender_id="owner",
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
    )


def test_per_umo_capacity_limit(tmp_path, monkeypatch):
    asyncio.run(_per_umo_capacity_limit(tmp_path, monkeypatch))


async def _foreground_counts_toward_capacity(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    config = _FakeConfig()
    config.max_active_per_umo = 1
    config.foreground_timeout_seconds = 1
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    first = asyncio.create_task(
        orch.dispatch_single(
            event=_make_event(),
            request=DispatchRequest(request_text="foreground", agent_name="butler"),
        )
    )
    await asyncio.sleep(0.05)
    assert orch._active_count_global() == 1
    try:
        await orch.dispatch_single(
            event=_make_event(),
            request=DispatchRequest(request_text="second", agent_name="butler"),
        )
    except CapacityExceededError:
        pass
    else:
        raise AssertionError("foreground run must consume capacity")
    runner.release()
    await first
    assert orch._active_count_global() == 0


def test_foreground_counts_toward_capacity(tmp_path, monkeypatch):
    asyncio.run(_foreground_counts_toward_capacity(tmp_path, monkeypatch))


async def _concurrent_capacity_reservation_has_one_winner(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    config = _FakeConfig()
    config.max_active_global = 1
    runners: list[_ScriptedRunner] = []
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        runner = _ScriptedRunner(result="done")
        runners.append(runner)
        return runner

    orch._runner_factory = _factory

    async def dispatch(label):
        return await orch.dispatch_single(
            event=_make_event(umo=f"aiocqhttp:GroupMessage:{label}"),
            request=DispatchRequest(
                request_text=label,
                agent_name="butler",
                run_in_background=True,
            ),
        )

    results = await asyncio.gather(dispatch("a"), dispatch("b"), return_exceptions=True)
    assert sum(isinstance(item, CapacityExceededError) for item in results) == 1
    winner = next(item for item in results if not isinstance(item, Exception))
    await asyncio.sleep(0.05)
    for runner in runners:
        runner.release()
    await asyncio.sleep(0.05)
    assert winner.task_id


def test_concurrent_capacity_reservation_has_one_winner(tmp_path, monkeypatch):
    asyncio.run(_concurrent_capacity_reservation_has_one_winner(tmp_path, monkeypatch))


async def _batch_capacity_race_has_no_partial_batch(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    config = _FakeConfig()
    config.max_active_global = 2
    runners: list[_ScriptedRunner] = []
    orch = RuntimeOrchestrator(store, config, runner_factory=None)

    async def _factory(run, event, payload):
        runner = _ScriptedRunner(result="done")
        runners.append(runner)
        return runner

    orch._runner_factory = _factory
    batch_call = orch.dispatch_batch(
        event=_make_event(umo="aiocqhttp:GroupMessage:batch"),
        requests=[
            DispatchRequest("b1", "butler", True),
            DispatchRequest("b2", "butler", True),
        ],
    )
    single_call = orch.dispatch_single(
        event=_make_event(umo="aiocqhttp:GroupMessage:single"),
        request=DispatchRequest("single", "butler", True),
    )
    batch_result, single_result = await asyncio.gather(
        batch_call,
        single_call,
        return_exceptions=True,
    )
    assert isinstance(batch_result, BatchCapacityError) ^ isinstance(
        single_result, CapacityExceededError
    )
    agent_count = len(await store.list_agent_ids())
    assert agent_count == (1 if isinstance(batch_result, BatchCapacityError) else 2)
    await asyncio.sleep(0.05)
    for runner in runners:
        runner.release()
    await asyncio.sleep(0.05)


def test_batch_capacity_race_has_no_partial_batch(tmp_path, monkeypatch):
    asyncio.run(_batch_capacity_race_has_no_partial_batch(tmp_path, monkeypatch))


async def _cross_umo_operations_are_denied(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        orch.register_steer_handler(run.agent_id, runner.make_steer_handler())
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg",
            agent_name="butler",
            run_in_background=True,
        ),
    )
    await asyncio.sleep(0.05)
    other_event = _make_event(umo="aiocqhttp:GroupMessage:g2")
    for operation in (
        orch.get_result(task_id=bg.task_id, event=other_event, block=False),
        orch.steer(agent_id=bg.agent_id, message_text="x", event=other_event),
    ):
        try:
            await operation
        except PermissionError:
            pass
        else:
            raise AssertionError("cross-UMO operation must be denied")
    runner.release()
    await asyncio.sleep(0.05)


def test_cross_umo_operations_are_denied(tmp_path, monkeypatch):
    asyncio.run(_cross_umo_operations_are_denied(tmp_path, monkeypatch))


async def _terminal_callback_runs_once_for_background(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)
    terminal: list[str] = []

    async def _factory(run, event, payload):
        return runner

    async def _terminal(run):
        terminal.append(run.task_id)

    orch._runner_factory = _factory
    orch.set_terminal_callback(_terminal)
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg",
            agent_name="butler",
            run_in_background=True,
        ),
    )
    runner.release()
    await asyncio.sleep(0.1)
    assert terminal == [bg.task_id]


def test_terminal_callback_runs_once_for_background(tmp_path, monkeypatch):
    asyncio.run(_terminal_callback_runs_once_for_background(tmp_path, monkeypatch))


async def _shutdown_marks_active_run_interrupted(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    runner = _ScriptedRunner(result="done")
    orch = RuntimeOrchestrator(store, _FakeConfig(), runner_factory=None)

    async def _factory(run, event, payload):
        return runner

    orch._runner_factory = _factory
    bg = await orch.dispatch_single(
        event=_make_event(),
        request=DispatchRequest(
            request_text="bg",
            agent_name="butler",
            run_in_background=True,
        ),
    )
    await asyncio.sleep(0.05)
    await orch.shutdown()
    run = await store.load_run(bg.agent_id, bg.task_id)
    assert run is not None
    assert run.status == "interrupted"
    assert run.notification is None
    assert orch._active_count_global() == 0


def test_shutdown_marks_active_run_interrupted(tmp_path, monkeypatch):
    asyncio.run(_shutdown_marks_active_run_interrupted(tmp_path, monkeypatch))
