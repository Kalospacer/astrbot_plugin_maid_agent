"""Claude Code-style foreground-first subagent runtime orchestration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from astrbot.api import logger

from .runtime_store import AgentMeta, RunMeta, RuntimeStore, _new_task_id

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .config import MaidModeConfig

STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_INTERRUPTED = "interrupted"

ACTIVE_STATUSES = frozenset({STATUS_STARTING, STATUS_RUNNING})
TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED, STATUS_INTERRUPTED}
)

MODE_FOREGROUND = "foreground"
MODE_BACKGROUND = "background"

BACKGROUND_REASON_TIMEOUT = "timeout"
BACKGROUND_REASON_EXPLICIT = "explicit"
BACKGROUND_REASON_RESUME = "resume"
BACKGROUND_REASON_BATCH = "batch"


class CapacityExceededError(RuntimeError):
    """Active-run capacity would be exceeded."""


class BatchCapacityError(CapacityExceededError):
    """A batch could not atomically reserve all required slots."""


class AgentBusyError(RuntimeError):
    """An agent already owns an active run."""


class RunNotFoundError(KeyError):
    """A referenced task or agent does not exist."""


@dataclass(slots=True)
class DispatchRequest:
    request_text: str
    agent_name: str = ""
    run_in_background: bool = False
    resume_agent_id: str = ""


@dataclass(slots=True)
class DispatchOutcome:
    agent_id: str
    task_id: str
    agent_name: str
    status: str
    mode: str
    background_reason: str = ""
    result: str = ""
    error: str = ""
    output_file: str = ""
    query_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "mode": self.mode,
            "background_reason": self.background_reason,
            "result": self.result,
            "error": self.error,
            "output_file": self.output_file,
        }
        if self.query_status:
            payload["query_status"] = self.query_status
        return payload


@dataclass(slots=True)
class BatchOutcome:
    batch_id: str
    items: list[DispatchOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "mode": "batch",
            "items": [item.to_dict() for item in self.items],
        }


class _ChildRunner(Protocol):
    async def run(self) -> str: ...


RunnerFactory = Callable[[RunMeta, "AstrMessageEvent", dict[str, Any]], Awaitable[_ChildRunner]]
TerminalCallback = Callable[[RunMeta], Awaitable[None]]


class RuntimeOrchestrator:
    """Owns capacity, active execution, migration, resume, steer, stop and result."""

    def __init__(
        self,
        store: RuntimeStore,
        config: MaidModeConfig,
        *,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._runner_factory = runner_factory
        self._terminal_callback: TerminalCallback | None = None
        self._lock = asyncio.Lock()
        self._active_tasks: dict[str, asyncio.Task[RunMeta | None]] = {}
        self._active_task_ids: dict[str, str] = {}
        self._task_to_agent: dict[str, str] = {}
        self._active_by_umo: dict[str, set[str]] = {}
        self._reserved_global = 0
        self._reserved_by_umo: dict[str, int] = {}
        self._steer_handlers: dict[str, Callable[[str], Awaitable[str]]] = {}
        self._stop_handlers: dict[str, Callable[[], None]] = {}
        self._runner_ready: dict[str, asyncio.Event] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._shutting_down = False

    def set_terminal_callback(self, callback: TerminalCallback) -> None:
        self._terminal_callback = callback

    @property
    def max_active_per_umo(self) -> int:
        return getattr(self.config, "max_active_per_umo", 5)

    @property
    def max_active_global(self) -> int:
        return getattr(self.config, "max_active_global", 20)

    @property
    def foreground_timeout_seconds(self) -> float:
        return float(getattr(self.config, "foreground_timeout_seconds", 50))

    def _active_count_global(self) -> int:
        return len(self._active_tasks) + self._reserved_global

    def _active_count_umo(self, unified_msg_origin: str) -> int:
        return len(self._active_by_umo.get(unified_msg_origin, set())) + self._reserved_by_umo.get(
            unified_msg_origin, 0
        )

    def _reserve_capacity_unlocked(self, unified_msg_origin: str, count: int) -> None:
        if count <= 0:
            return
        if self._active_count_global() + count > self.max_active_global:
            raise CapacityExceededError(
                f"全局活跃 run 数将超上限: {self._active_count_global()}+{count}>{self.max_active_global}"
            )
        if self._active_count_umo(unified_msg_origin) + count > self.max_active_per_umo:
            raise CapacityExceededError(
                f"会话活跃 run 数将超上限: {self._active_count_umo(unified_msg_origin)}+{count}"
                f">{self.max_active_per_umo}"
            )
        self._reserved_global += count
        self._reserved_by_umo[unified_msg_origin] = (
            self._reserved_by_umo.get(unified_msg_origin, 0) + count
        )

    def _release_reservation_unlocked(self, unified_msg_origin: str, count: int = 1) -> None:
        if count <= 0:
            return
        self._reserved_global = max(0, self._reserved_global - count)
        remaining = max(0, self._reserved_by_umo.get(unified_msg_origin, 0) - count)
        if remaining:
            self._reserved_by_umo[unified_msg_origin] = remaining
        else:
            self._reserved_by_umo.pop(unified_msg_origin, None)

    def _register_active_unlocked(
        self,
        agent_id: str,
        task_id: str,
        unified_msg_origin: str,
        task: asyncio.Task[RunMeta | None],
    ) -> None:
        if agent_id in self._active_tasks:
            raise AgentBusyError(f"agent 已有活跃 run: {agent_id}")
        self._release_reservation_unlocked(unified_msg_origin)
        self._active_tasks[agent_id] = task
        self._active_task_ids[agent_id] = task_id
        self._task_to_agent[task_id] = agent_id
        self._active_by_umo.setdefault(unified_msg_origin, set()).add(agent_id)

    def _release_active_unlocked(self, agent_id: str) -> None:
        task_id = self._active_task_ids.pop(agent_id, "")
        self._active_tasks.pop(agent_id, None)
        self._steer_handlers.pop(agent_id, None)
        self._stop_handlers.pop(agent_id, None)
        self._runner_ready.pop(agent_id, None)
        if task_id:
            self._task_to_agent.pop(task_id, None)
            self._completion_events.pop(task_id, None)
        for umo, agents in list(self._active_by_umo.items()):
            agents.discard(agent_id)
            if not agents:
                self._active_by_umo.pop(umo, None)

    async def dispatch_single(
        self,
        *,
        event: AstrMessageEvent,
        request: DispatchRequest,
        runner_payload: dict[str, Any] | None = None,
    ) -> DispatchOutcome:
        request_text = (request.request_text or "").strip()
        if not request_text:
            raise ValueError("request_text 不能为空。")
        resume_agent_id = (request.resume_agent_id or "").strip().casefold()
        if resume_agent_id:
            return await self._dispatch_resume(
                event=event,
                agent_id=resume_agent_id,
                request_text=request_text,
                runner_payload=runner_payload or {},
            )
        return await self._dispatch_new(
            event=event,
            agent_name=request.agent_name or self._default_agent_name(),
            request_text=request_text,
            run_in_background=request.run_in_background,
            runner_payload=runner_payload or {},
        )

    async def dispatch_batch(
        self,
        *,
        event: AstrMessageEvent,
        requests: list[DispatchRequest],
        runner_payload: dict[str, Any] | None = None,
    ) -> BatchOutcome:
        if not requests:
            raise ValueError("batch 不能为空。")
        if len(requests) > 5:
            raise ValueError(f"batch 最多 5 项，收到 {len(requests)}。")
        normalized: list[DispatchRequest] = []
        for request in requests:
            text = (request.request_text or "").strip()
            if not text:
                raise ValueError("batch 项的 request_text 不能为空。")
            if request.resume_agent_id:
                raise ValueError("batch 不允许 resume_agent_id。")
            normalized.append(
                DispatchRequest(
                    request_text=text,
                    agent_name=request.agent_name or self._default_agent_name(),
                    run_in_background=bool(request.run_in_background),
                )
            )

        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        created: list[tuple[AgentMeta, RunMeta, DispatchRequest]] = []
        async with self._lock:
            try:
                self._reserve_capacity_unlocked(umo, len(normalized))
            except CapacityExceededError as exc:
                raise BatchCapacityError(str(exc)) from exc
            try:
                for request in normalized:
                    agent = await self.store.create_agent(
                        unified_msg_origin=umo,
                        agent_name=request.agent_name,
                        sender_id=sender_id,
                    )
                    mode = MODE_BACKGROUND if request.run_in_background else MODE_FOREGROUND
                    run = await self.store.create_run(
                        RunMeta(
                            task_id=_new_task_id(),
                            agent_id=agent.agent_id,
                            unified_msg_origin=umo,
                            agent_name=request.agent_name,
                            sender_id=sender_id,
                            mode=mode,
                            status=STATUS_STARTING,
                            request_text=request.request_text,
                            background_reason=(
                                BACKGROUND_REASON_BATCH if request.run_in_background else ""
                            ),
                        )
                    )
                    await self.store.set_active_task(agent.agent_id, run.task_id, STATUS_STARTING)
                    created.append((agent, run, request))
            except Exception:
                self._release_reservation_unlocked(umo, len(normalized))
                raise

        started = [
            await self._start_execution(agent, run, event, runner_payload or {})
            for agent, run, _ in created
        ]
        waits = []
        for (agent, run, request), task in zip(created, started):
            if request.run_in_background:
                waits.append(
                    asyncio.sleep(
                        0,
                        result=self._outcome_from_run(run, status=STATUS_STARTING),
                    )
                )
            else:
                waits.append(self._await_foreground(agent, run, task))
        items = list(await asyncio.gather(*waits))
        return BatchOutcome(batch_id=uuid.uuid4().hex, items=items)

    async def _dispatch_new(
        self,
        *,
        event: AstrMessageEvent,
        agent_name: str,
        request_text: str,
        run_in_background: bool,
        runner_payload: dict[str, Any],
    ) -> DispatchOutcome:
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        async with self._lock:
            self._reserve_capacity_unlocked(umo, 1)
            try:
                agent = await self.store.create_agent(
                    unified_msg_origin=umo,
                    agent_name=agent_name,
                    sender_id=sender_id,
                )
                mode = MODE_BACKGROUND if run_in_background else MODE_FOREGROUND
                run = await self.store.create_run(
                    RunMeta(
                        task_id=_new_task_id(),
                        agent_id=agent.agent_id,
                        unified_msg_origin=umo,
                        agent_name=agent_name,
                        sender_id=sender_id,
                        mode=mode,
                        status=STATUS_STARTING,
                        request_text=request_text,
                        background_reason=(
                            BACKGROUND_REASON_EXPLICIT if run_in_background else ""
                        ),
                    )
                )
                await self.store.set_active_task(agent.agent_id, run.task_id, STATUS_STARTING)
            except Exception:
                self._release_reservation_unlocked(umo)
                raise
        task = await self._start_execution(agent, run, event, runner_payload)
        if run_in_background:
            return self._outcome_from_run(run, status=STATUS_STARTING)
        return await self._await_foreground(agent, run, task)

    async def _dispatch_resume(
        self,
        *,
        event: AstrMessageEvent,
        agent_id: str,
        request_text: str,
        runner_payload: dict[str, Any],
    ) -> DispatchOutcome:
        meta = await self.store.load_agent(agent_id)
        if meta is None:
            raise RunNotFoundError(f"未找到 agent: {agent_id}")
        self._authorize(meta.unified_msg_origin, meta.sender_id, event)
        active = self._active_tasks.get(agent_id)
        if active is not None and not active.done():
            try:
                ticket = await self.steer(
                    agent_id=agent_id,
                    message_text=request_text,
                    event=event,
                )
            except RunNotFoundError:
                pass
            else:
                return DispatchOutcome(
                    agent_id=agent_id,
                    task_id=self._active_task_ids.get(agent_id, meta.active_task_id),
                    agent_name=meta.agent_name,
                    status=STATUS_RUNNING,
                    mode=MODE_BACKGROUND,
                    background_reason="steer",
                    result=ticket,
                )

        async with self._lock:
            if agent_id in self._active_tasks:
                raise AgentBusyError(f"agent 已有活跃 run: {agent_id}")
            self._reserve_capacity_unlocked(meta.unified_msg_origin, 1)
            try:
                run = await self.store.create_run(
                    RunMeta(
                        task_id=_new_task_id(),
                        agent_id=agent_id,
                        unified_msg_origin=meta.unified_msg_origin,
                        agent_name=meta.agent_name,
                        sender_id=meta.sender_id,
                        mode=MODE_BACKGROUND,
                        status=STATUS_STARTING,
                        request_text=request_text,
                        resume_of=agent_id,
                        background_reason=BACKGROUND_REASON_RESUME,
                    )
                )
                await self.store.set_active_task(agent_id, run.task_id, STATUS_STARTING)
            except Exception:
                self._release_reservation_unlocked(meta.unified_msg_origin)
                raise
        await self._start_execution(meta, run, event, runner_payload)
        return self._outcome_from_run(run, status=STATUS_STARTING)

    async def _start_execution(
        self,
        agent: AgentMeta,
        run: RunMeta,
        event: AstrMessageEvent,
        runner_payload: dict[str, Any],
    ) -> asyncio.Task[RunMeta | None]:
        self._runner_ready[agent.agent_id] = asyncio.Event()
        self._completion_events[run.task_id] = asyncio.Event()
        start_gate = asyncio.Event()
        task = asyncio.create_task(
            self._execute_run(run, event, runner_payload, start_gate),
            name=f"maid-{run.mode}-{run.task_id[:8]}",
        )
        async with self._lock:
            self._register_active_unlocked(
                agent.agent_id,
                run.task_id,
                run.unified_msg_origin,
                task,
            )
        start_gate.set()
        return task

    async def _execute_run(
        self,
        run: RunMeta,
        event: AstrMessageEvent,
        runner_payload: dict[str, Any],
        start_gate: asyncio.Event,
    ) -> RunMeta | None:
        try:
            await start_gate.wait()
            if self._runner_factory is None:
                raise RuntimeError("runner_factory 未注入，无法执行 run。")
            await self.store.update_run(
                run.agent_id,
                run.task_id,
                status=STATUS_RUNNING,
                started_at=self.store.iso_now(),
            )
            runner = await self._runner_factory(run, event, runner_payload)
            result_text = await runner.run()
            finalized = await self.store.finalize_run(
                run.agent_id,
                run.task_id,
                status=STATUS_COMPLETED,
                result=result_text,
            )
            await self._notify_terminal(finalized)
            return finalized
        except asyncio.CancelledError:
            if self._shutting_down:
                await self.store.interrupt_run(run.agent_id, run.task_id)
            else:
                current = await self.store.load_run(run.agent_id, run.task_id)
                if current is None or current.status not in TERMINAL_STATUSES:
                    current = await self.store.finalize_run(
                        run.agent_id,
                        run.task_id,
                        status=STATUS_STOPPED,
                        error="执行已取消",
                    )
                    await self._notify_terminal(current)
            raise
        except Exception as exc:
            logger.error(
                "[大小姐模式] runtime run 失败: agent_id=%s task_id=%s err=%s",
                run.agent_id,
                run.task_id,
                exc,
                exc_info=True,
            )
            finalized = await self.store.finalize_run(
                run.agent_id,
                run.task_id,
                status=STATUS_FAILED,
                error=str(exc),
            )
            await self._notify_terminal(finalized)
            return finalized
        finally:
            ready = self._runner_ready.get(run.agent_id)
            if ready is not None:
                ready.set()
            completion = self._completion_events.get(run.task_id)
            if completion is not None:
                completion.set()
            async with self._lock:
                self._release_active_unlocked(run.agent_id)

    async def _await_foreground(
        self,
        agent: AgentMeta,
        run: RunMeta,
        task: asyncio.Task[RunMeta | None],
    ) -> DispatchOutcome:
        try:
            finalized = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.foreground_timeout_seconds,
            )
        except asyncio.TimeoutError:
            updated = await self.store.update_run(
                agent.agent_id,
                run.task_id,
                mode=MODE_BACKGROUND,
                background_reason=BACKGROUND_REASON_TIMEOUT,
            )
            return self._outcome_from_run(updated or run, status=STATUS_RUNNING)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if finalized is None:
            return self._outcome_from_run(run, status=STATUS_FAILED, error="run 元数据丢失")
        return self._outcome_from_run(finalized)

    async def _notify_terminal(self, run: RunMeta | None) -> None:
        if run is None or self._terminal_callback is None:
            return
        if run.mode != MODE_BACKGROUND:
            return
        try:
            await self._terminal_callback(run)
        except Exception as exc:
            logger.warning(
                "[大小姐模式] 触发 terminal notification 失败: task_id=%s err=%s",
                run.task_id,
                exc,
            )

    async def steer(
        self,
        *,
        agent_id: str,
        message_text: str,
        event: AstrMessageEvent | None = None,
        sender_id: str = "",
        unified_msg_origin: str = "",
    ) -> str:
        text = (message_text or "").strip()
        if not text:
            raise ValueError("steer 的 message 不能为空。")
        meta = await self.store.load_agent(agent_id)
        if meta is None:
            raise RunNotFoundError(f"未找到 agent: {agent_id}")
        self._authorize_values(
            meta.unified_msg_origin,
            meta.sender_id,
            event=event,
            sender_id=sender_id,
            unified_msg_origin=unified_msg_origin,
        )
        handler = self._steer_handlers.get(agent_id)
        if handler is None and agent_id in self._active_tasks:
            ready = self._runner_ready.get(agent_id)
            if ready is not None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ready.wait(), timeout=5)
            handler = self._steer_handlers.get(agent_id)
        if handler is None:
            raise RunNotFoundError(f"该 agent 当前不在运行中，无法 steer: {agent_id}")
        ticket = await handler(text)
        task_id = self._active_task_ids.get(agent_id, meta.active_task_id)
        await self.store.append_control(
            agent_id,
            "steer",
            {"task_id": task_id, "message": text, "message_id": ticket},
        )
        return ticket

    def register_steer_handler(
        self,
        agent_id: str,
        handler: Callable[[str], Awaitable[str]],
    ) -> None:
        self._steer_handlers[agent_id] = handler
        ready = self._runner_ready.get(agent_id)
        if ready is not None:
            ready.set()

    def unregister_steer_handler(self, agent_id: str) -> None:
        self._steer_handlers.pop(agent_id, None)

    def register_stop_handler(self, agent_id: str, handler: Callable[[], None]) -> None:
        self._stop_handlers[agent_id] = handler

    async def stop(
        self,
        *,
        task_id: str,
        event: AstrMessageEvent | None = None,
        sender_id: str = "",
        unified_msg_origin: str = "",
        source: str = "chat",
    ) -> DispatchOutcome:
        found = await self.store.find_run(task_id)
        if found is None:
            raise RunNotFoundError(f"未找到 task: {task_id}")
        agent, run = found
        if source == "chat":
            self._authorize_values(
                run.unified_msg_origin,
                run.sender_id,
                event=event,
                sender_id=sender_id,
                unified_msg_origin=unified_msg_origin,
            )
        active_task_id = self._active_task_ids.get(agent.agent_id)
        if active_task_id != task_id:
            return self._outcome_from_run(run)
        stop_handler = self._stop_handlers.get(agent.agent_id)
        if stop_handler is not None:
            stop_handler()
        finalized = await self.store.finalize_run(
            agent.agent_id,
            task_id,
            status=STATUS_STOPPED,
            error="已请求停止",
        )
        await self._notify_terminal(finalized)
        task = self._active_tasks.get(agent.agent_id)
        if task is not None and not task.done():
            task.cancel()
        return self._outcome_from_run(finalized or run, status=STATUS_STOPPED)

    async def get_result(
        self,
        *,
        task_id: str,
        event: AstrMessageEvent | None = None,
        sender_id: str = "",
        unified_msg_origin: str = "",
        block: bool = True,
        timeout_ms: int = 30000,
        agent_id: str = "",
    ) -> DispatchOutcome:
        found = await self.store.find_run(task_id)
        if found is None:
            raise RunNotFoundError(f"未找到 task: {task_id}")
        agent, run = found
        if agent_id and agent.agent_id != agent_id:
            raise RunNotFoundError(f"task_id={task_id} 不属于 agent_id={agent_id}")
        self._authorize_values(
            run.unified_msg_origin,
            run.sender_id,
            event=event,
            sender_id=sender_id,
            unified_msg_origin=unified_msg_origin,
        )
        if run.status in TERMINAL_STATUSES:
            return self._outcome_from_run(run, query_status="success")
        if not block or int(timeout_ms) <= 0:
            return self._outcome_from_run(run, query_status="not_ready")
        timeout_ms = min(max(int(timeout_ms), 1), 600_000)
        completion = self._completion_events.get(task_id)
        if completion is None:
            current = await self.store.load_run(agent.agent_id, task_id)
            if current is not None and current.status in TERMINAL_STATUSES:
                return self._outcome_from_run(current, query_status="success")
            return self._outcome_from_run(current or run, query_status="not_ready")
        try:
            await asyncio.wait_for(completion.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            current = await self.store.load_run(agent.agent_id, task_id)
            return self._outcome_from_run(current or run, query_status="timeout")
        current = await self.store.load_run(agent.agent_id, task_id)
        if current is None:
            raise RunNotFoundError(f"run 在等待期间消失: {task_id}")
        return self._outcome_from_run(
            current,
            query_status=("success" if current.status in TERMINAL_STATUSES else "not_ready"),
        )

    async def list_active_runs(
        self,
        unified_msg_origin: str,
        sender_id: str = "",
    ) -> list[RunMeta]:
        task_ids = [
            task_id
            for agent_id, task_id in self._active_task_ids.items()
            if agent_id in self._active_by_umo.get(unified_msg_origin, set())
        ]
        runs: list[RunMeta] = []
        for task_id in task_ids:
            found = await self.store.find_run(task_id)
            if found is None:
                continue
            _agent, run = found
            if sender_id and run.sender_id and run.sender_id != sender_id:
                continue
            runs.append(run)
        return runs

    def _authorize(self, umo: str, sender_id: str, event: AstrMessageEvent) -> None:
        self._authorize_values(umo, sender_id, event=event)

    @staticmethod
    def _authorize_values(
        expected_umo: str,
        expected_sender: str,
        *,
        event: AstrMessageEvent | None = None,
        sender_id: str = "",
        unified_msg_origin: str = "",
    ) -> None:
        actual_umo = event.unified_msg_origin if event is not None else unified_msg_origin
        actual_sender = event.get_sender_id() if event is not None else sender_id
        if event is None and not sender_id and not unified_msg_origin:
            return
        if expected_umo and expected_umo != actual_umo:
            raise PermissionError("该任务不属于当前会话，无法操作。")
        if expected_sender and expected_sender != actual_sender:
            raise PermissionError("该任务不属于当前发言者，无法操作。")

    @staticmethod
    def _outcome_from_run(
        run: RunMeta,
        *,
        status: str = "",
        error: str = "",
        query_status: str = "",
    ) -> DispatchOutcome:
        return DispatchOutcome(
            agent_id=run.agent_id,
            task_id=run.task_id,
            agent_name=run.agent_name,
            status=status or run.status,
            mode=run.mode,
            background_reason=run.background_reason,
            result=run.result,
            error=error or run.error,
            output_file=run.output_file,
            query_status=query_status,
        )

    def _default_agent_name(self) -> str:
        return getattr(self.config, "default_agent_name", "butler")

    async def shutdown(self) -> None:
        """Cancel all live runners and persist them as silent interrupted runs."""
        self._shutting_down = True
        tasks = [task for task in self._active_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            for agent_id in list(self._active_tasks):
                self._release_active_unlocked(agent_id)
            self._reserved_global = 0
            self._reserved_by_umo.clear()
