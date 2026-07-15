"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 主模型不直接暴露任何原生工具
- 需要幕后执行时通过原生 `call_maid` function call 调度管家
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import suppress
from dataclasses import asdict
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from quart import Response as QuartResponse
from quart import jsonify, make_response, request

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallMessageSegment,
)
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astr_message_event import AstrMessageEvent as CoreAstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ToolCallsResult
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.history_saver import persist_agent_history

from .background_registry import MaidBackgroundTaskRegistry, MaidTaskConflictError
from .batch_registry import MaidBatchRegistry
from .config import _safe_int, load_maid_mode_config, render_dispatch_prompt
from .console_store import ConsoleTaskPatch, MaidConsoleEventStore
from .constants import (
    CALL_MAID_TOOL_NAME,
    MAID_AGENT_ID_META_KEY,
    MAID_NOTIFICATION_ID_META_KEY,
    MAID_NOTIFICATION_IDS_META_KEY,
    MAID_TASK_TOOL_NAME,
    PENDING_MAID_DISPATCHES_EXTRA_KEY,
    PENDING_MAID_FOLLOW_UP_EXTRA_KEY,
    PENDING_MAID_TOOL_HISTORY_EXTRA_KEY,
    PLUGIN_DATA_DIR_NAME,
    RAW_INPUT_EXTRA_KEY,
    TRUE_USER_INPUT_EXTRA_KEY,
)
from .maid_dispatcher import (
    _get_compress_provider,
    _normalize_begin_dialogs,
    dispatch_to_maid_agent,
)
from .notification_outbox import (
    NotificationOutbox,
    NotifierResult,
)
from .runtime_orchestrator import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_STOPPED,
    AgentBusyError,
    BatchCapacityError,
    CapacityExceededError,
    DispatchRequest,
    PendingNotificationError,
    RunNotFoundError,
    RuntimeOrchestrator,
)
from .runtime_store import RunMeta, RuntimeStore
from .session_store import MaidSessionStore
from .toolset_adapter import (
    _agent_memory_enabled as _agent_memory_enabled_fn,
    _load_provider_settings as _load_provider_settings_fn,
    build_child_toolset,
    collect_child_image_urls,
    get_memory_dir,
    load_memory_index_inline,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context

_EMPTY_REASONING_PLACEHOLDER = " "
_CONSOLE_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_CONSOLE_IMAGE_MAX_COUNT = 5
_CONSOLE_IMAGE_MIME_TYPES = {
    "jpeg": frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    "png": frozenset({"image/png"}),
    "gif": frozenset({"image/gif"}),
    "webp": frozenset({"image/webp"}),
}
_CONSOLE_IMAGE_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
}
_CONSOLE_IMAGE_CANONICAL_MIME = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


class _DashboardMessage(AstrBotMessage):
    def __init__(
        self,
        *,
        text: str,
        sender_id: str,
        session: MessageSession,
    ) -> None:
        super().__init__()
        self.type = session.message_type
        self.self_id = "dashboard"
        self.session_id = session.session_id
        self.message_id = f"dashboard_{uuid.uuid4().hex}"
        self.group_id = session.session_id if session.message_type.value == "GroupMessage" else ""
        self.sender = MessageMember(user_id=sender_id, nickname="Dashboard")
        self.message = []
        self.message_str = text
        self.raw_message = {"source": "dashboard", "text": text}
        self.image_urls = []


class _DashboardMaidEvent(CoreAstrMessageEvent):
    """Minimal AstrMessageEvent-compatible object for dashboard-triggered runs."""

    def __init__(self, *, unified_msg_origin: str, sender_id: str, message_text: str) -> None:
        session = MessageSession.from_str(unified_msg_origin)
        message_obj = _DashboardMessage(
            text=message_text,
            sender_id=sender_id,
            session=session,
        )
        platform_meta = PlatformMetadata(
            name="dashboard",
            description="AstrBot Dashboard",
            id=session.platform_id,
            support_streaming_message=False,
            support_proactive_message=False,
        )
        super().__init__(
            message_str=message_text,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session.session_id,
        )
        self.role = "admin"
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.call_llm = False
        self.plugins_name = None
        self.sent_messages: list[str] = []

    async def send(self, message: MessageChain) -> None:
        try:
            text = message.get_plain_text()
        except Exception:
            text = str(message)
        self.sent_messages.append(text)


class _IsolatedMaidEvent(CoreAstrMessageEvent):
    """Child event with copied identity and independent mutable event state."""

    def __init__(self, original: AstrMessageEvent) -> None:
        message_obj = copy.copy(original.message_obj)
        message_obj.message = list(getattr(original.message_obj, "message", []) or [])
        message_obj.sender = copy.copy(getattr(original.message_obj, "sender", None))
        super().__init__(
            message_str="",
            message_obj=message_obj,
            platform_meta=copy.copy(original.platform_meta),
            session_id=original.session_id,
        )
        self.role = getattr(original, "role", "member")
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.call_llm = False
        plugins_name = getattr(original, "plugins_name", None)
        self.plugins_name = list(plugins_name) if isinstance(plugins_name, list) else plugins_name
        self.sent_messages: list[str] = []

    async def send(self, message: MessageChain) -> None:
        try:
            text = message.get_plain_text()
        except Exception:
            text = str(message)
        self.sent_messages.append(text)


class _ChildRunner:
    """Holds the subagent execution coroutine for the orchestrator.

    The orchestrator calls ``run()``; for foreground runs it is awaited
    inline (with a 50s wait_for budget), for background runs it runs to
    completion in a task.
    """

    __slots__ = ("_coro",)

    def __init__(self, coro) -> None:
        self._coro = coro

    async def run(self) -> str:
        return await self._coro()


class _RuntimeTraceHooks(BaseAgentRunHooks):
    """Persist tool start/end controls before runner messages are finalized."""

    __slots__ = (
        "_active_call_id",
        "_active_tool_name",
        "_publish",
        "_run",
        "_sequence",
        "_store",
    )

    def __init__(self, run: RunMeta, store: RuntimeStore, publish) -> None:
        self._run = run
        self._store = store
        self._publish = publish
        self._sequence = 0
        self._active_call_id = ""
        self._active_tool_name = ""

    async def on_tool_start(self, _run_context, tool, tool_args) -> None:
        self._sequence += 1
        self._active_call_id = f"runtime_{self._run.task_id}_{self._sequence}"
        self._active_tool_name = str(getattr(tool, "name", "") or "")
        await self._store.append_control(
            self._run.agent_id,
            "tool_start",
            {
                "task_id": self._run.task_id,
                "tool_call_id": self._active_call_id,
                "tool_name": self._active_tool_name,
                "arguments": tool_args or {},
            },
        )
        await self._publish()

    async def on_tool_end(self, _run_context, tool, _tool_args, tool_result) -> None:
        call_id = self._active_call_id
        if not call_id:
            self._sequence += 1
            call_id = f"runtime_{self._run.task_id}_{self._sequence}"
        await self._store.append_control(
            self._run.agent_id,
            "tool_end",
            {
                "task_id": self._run.task_id,
                "tool_call_id": call_id,
                "tool_name": str(getattr(tool, "name", "") or ""),
                "result": MaidAgent._runtime_tool_result_to_text(tool_result),
            },
        )
        self._active_call_id = ""
        self._active_tool_name = ""
        await self._publish()

    async def close_unfinished_tool(self) -> None:
        """Close a start record when Core skipped on_tool_end after an error."""
        if not self._active_call_id:
            return
        await self._store.append_control(
            self._run.agent_id,
            "tool_end",
            {
                "task_id": self._run.task_id,
                "tool_call_id": self._active_call_id,
                "tool_name": self._active_tool_name,
                "result": "error: 工具执行异常结束，未收到正常结束回调。",
            },
        )
        self._active_call_id = ""
        self._active_tool_name = ""
        await self._publish()


def _max_step_message():
    from astrbot.core.agent.message import Message

    return Message(
        role="user",
        content="工具调用次数已达到上限，请停止使用工具，并根据已经收集到的信息，对你的任务和发现进行总结，然后直接回复对方。",
    )


class MaidAgent(Star):
    """大小姐管家模式插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.maid_mode_config = load_maid_mode_config(self.config)
        self.session_store: MaidSessionStore | None = None
        self.console_store = MaidConsoleEventStore()
        self.background_tasks = MaidBackgroundTaskRegistry()
        self.batch_registry = MaidBatchRegistry()
        self._active_asyncio_tasks: set[asyncio.Task] = set()
        self._background_runners_by_umo: dict[str, object] = {}
        self._background_runner_events_by_runner_id: dict[int, AstrMessageEvent] = {}
        self._batch_runners_by_batch_id: dict[str, dict[int, object]] = {}
        self._stop_requested_batch_ids: set[str] = set()
        self._conversation_history_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        # 1.3.0 runtime: foreground-first subagent orchestration.
        self.runtime_store = RuntimeStore(self.maid_mode_config)
        self.orchestrator = RuntimeOrchestrator(
            self.runtime_store,
            self.maid_mode_config,
            runner_factory=self._make_child_runner,
        )
        self.outbox = NotificationOutbox(self.runtime_store)
        self.outbox.set_notifier(self._notify_main_agent)
        self.outbox.set_history_scanner(self._scan_history_for_dedupe)
        self.orchestrator.set_terminal_callback(self._on_runtime_terminal)
        self._child_runner_steer_handlers: dict[str, object] = {}

    async def initialize(self) -> None:
        """插件初始化"""
        self.session_store = MaidSessionStore(self, self.maid_mode_config)
        await self.console_store.initialize()
        reconciled_task_ids = await self.console_store.reconcile_incomplete_tasks()
        if reconciled_task_ids:
            logger.warning(
                "[大小姐模式] 已收敛 %s 个因插件重启遗留的后台任务: %s",
                len(reconciled_task_ids),
                ",".join(task_id[:8] for task_id in reconciled_task_ids),
            )
        # 1.3.0 runtime: collapse orphaned runs and retry pending notifications.
        reconciled_runs = await self.runtime_store.reconcile_on_restart()
        if reconciled_runs:
            await self.console_store_reconcile_runtime(reconciled_runs)
        await self.outbox.on_restart()
        self._patch_runtime_tool_schemas()
        self._schedule_retention_cleanup()
        self._register_console_web_apis()
        logger.info(
            "[MaidAgent] 已加载 (1.3.0) | default_agent=%s | allowed_agents=%s | hide_native_tools=%s | hide_transfer_tools=%s | include_raw_user_input=%s | log_raw_llm_io=%s | fg_timeout=%ss | memory_agents=%s | capacity=%s/%s | retention=%dd",
            self.maid_mode_config.default_agent_name,
            ",".join(self.maid_mode_config.allowed_agent_names or []),
            self.maid_mode_config.hide_native_tools,
            self.maid_mode_config.hide_transfer_tools,
            self.maid_mode_config.include_raw_user_input,
            self.maid_mode_config.log_raw_llm_io,
            self.maid_mode_config.foreground_timeout_seconds,
            ",".join(self.maid_mode_config.memory_agent_names or []),
            self.maid_mode_config.max_active_per_umo,
            self.maid_mode_config.max_active_global,
            self.maid_mode_config.retention_days,
        )

    async def terminate(self) -> None:
        """插件停用/重载时停止后台 runner 并取消未完成任务。"""
        await self.orchestrator.shutdown()
        await self.outbox.shutdown()
        runners = list(self._background_runners_by_umo.values())
        for runner_map in self._batch_runners_by_batch_id.values():
            runners.extend(runner_map.values())

        for runner in runners:
            runner_event = self._background_runner_events_by_runner_id.get(id(runner))
            if runner_event is not None:
                runner_event.set_extra("agent_stop_requested", True)
            try:
                runner.request_stop()
            except Exception as exc:
                logger.warning("[大小姐模式] terminate 阶段停止 runner 失败: %s", exc)

        tasks = [task for task in self._active_asyncio_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._active_asyncio_tasks.clear()
        self._background_runners_by_umo.clear()
        self._background_runner_events_by_runner_id.clear()
        self._batch_runners_by_batch_id.clear()
        self._stop_requested_batch_ids.clear()
        await self.console_store.close()

    async def _on_runtime_terminal(self, run: RunMeta) -> None:
        notification_id = run.notification.notification_id if run.notification else ""
        existing_task = None
        try:
            existing_task = await self.console_store.get_task(run.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[大小姐模式] 读取终态 Console 任务失败，将使用 runtime 默认字段: "
                "task_id=%s err=%s",
                run.task_id,
                exc,
            )
        audit_patch = self._build_task_patch(
            task_id=run.task_id,
            kind=str((existing_task or {}).get("kind") or "single"),
            source=str((existing_task or {}).get("source") or "runtime"),
            unified_msg_origin=run.unified_msg_origin,
            sender_id=run.sender_id,
            agent_name=run.agent_name,
            status=run.status,
            request_text=run.request_text,
            parent_task_id=str((existing_task or {}).get("parent_task_id") or ""),
            title=str((existing_task or {}).get("title") or ""),
            meta={
                "agent_id": run.agent_id,
                "run_mode": run.mode,
                "background_reason": run.background_reason,
                "notification_id": notification_id,
            },
        )
        await self._console_ensure_task_safe(audit_patch)
        await self._console_update_status_safe(
            run.task_id,
            run.status,
            meta={
                "agent_id": run.agent_id,
                "run_mode": run.mode,
                "background_reason": run.background_reason,
                "notification_id": notification_id,
            },
        )
        await self._console_event_safe(
            task_id=run.task_id,
            event_type="runtime_terminal",
            title=f"Run {run.status}",
            message=run.result or run.error,
            source="runtime",
            status=run.status,
            payload={
                "agent_id": run.agent_id,
                "notification_id": notification_id,
            },
        )
        await self.outbox.queue_delivery(run.unified_msg_origin)

    def _patch_runtime_tool_schemas(self) -> None:
        """Install the nested JSON Schema that AstrBot's doc parser cannot express."""
        manager = self.context.get_llm_tool_manager()
        call_tool = manager.get_func(CALL_MAID_TOOL_NAME) if manager else None
        if call_tool is not None:
            call_tool.parameters = {
                "type": "object",
                "properties": {
                    "request_text": {
                        "type": "string",
                        "description": "Self-contained task request for one agent.",
                    },
                    "agent_name": {"type": "string"},
                    "resume_agent_id": {"type": "string"},
                    "run_in_background": {"type": "boolean", "default": False},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "request_text": {"type": "string", "minLength": 1},
                                "agent_name": {"type": "string"},
                                "run_in_background": {
                                    "type": "boolean",
                                    "default": False,
                                },
                            },
                            "required": ["request_text"],
                        },
                    },
                    "action": {
                        "type": "string",
                        "enum": ["", "dispatch", "steer", "stop", "done"],
                        "description": "Deprecated 1.2 compatibility field.",
                    },
                },
            }
        task_tool = manager.get_func(MAID_TASK_TOOL_NAME) if manager else None
        if task_tool is not None:
            task_tool.parameters = {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "result", "stop", "steer"],
                    },
                    "task_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "message": {"type": "string"},
                    "block": {"type": "boolean", "default": True},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 600000,
                        "default": 30000,
                    },
                },
                "required": ["action"],
            }

    # ==================================================================
    # 1.3.0 runtime: child runner factory, notification wake, helpers
    # ==================================================================

    def _schedule_retention_cleanup(self) -> None:
        """Best-effort periodic pruning of inactive runtime agents."""

        async def _loop() -> None:
            while True:
                await asyncio.sleep(3600)
                try:
                    removed = await self.runtime_store.prune_inactive(
                        self.maid_mode_config.retention_days
                    )
                    if removed:
                        logger.info("[大小姐模式] runtime retention 清理 %d 个 agent", removed)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[大小姐模式] retention 清理失败: %s", exc)

        task = asyncio.create_task(_loop(), name="maid-retention-loop")
        self._track_background_task(task)

    async def console_store_reconcile_runtime(self, runs: list) -> None:
        """Surface interrupted 1.3.0 runs into the SQLite audit store."""
        for run in runs:
            try:
                await self._console_update_status_safe(run.task_id, "interrupted")
                await self._console_event_safe(
                    task_id=run.task_id,
                    event_type="interrupted",
                    title="插件重启时仍在运行",
                    message="已标记为 interrupted，不自动重放。",
                    source="system",
                    status="interrupted",
                    payload={"agent_id": run.agent_id},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[大小姐模式] 收敛 runtime run 到 console 失败: %s", exc)

    async def _make_child_runner(self, run: RunMeta, event, payload: dict):
        """Build a _ChildRunner that executes the subagent loop for a run.

        Uses maid_dispatcher._build_runner so we can hold the runner handle and
        register a steer handler (runner.follow_up) on it.
        """
        context = self.context
        agent_name = run.agent_name or self.maid_mode_config.default_agent_name
        umo = run.unified_msg_origin
        handoff, resolved_name = self._resolve_handoff_for_runtime(agent_name)
        request_text = run.request_text

        # Child event isolation: keep UMO/sender/role/group/platform, fresh extras.
        child_event = self._isolate_child_event(event)

        # Resolve provider + provider settings for the handoff.
        provider_id = getattr(handoff, "provider_id", None) or await context.get_current_chat_provider_id(umo)
        provider = context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到子 agent provider: {provider_id}")

        toolset = build_child_toolset(
            context,
            handoff=handoff,
            umo=umo,
            agent_name=resolved_name,
            memory_agent_names=self.maid_mode_config.memory_agent_names,
        )

        # Resume: rebuild contexts from the agent's transcript.
        resumed_contexts = None
        if run.resume_of:
            resumed_contexts = await self.runtime_store.rebuild_contexts_for_resume(run.agent_id)
        initial_contexts = (
            resumed_contexts
            if run.resume_of
            else _normalize_begin_dialogs(getattr(handoff.agent, "begin_dialogs", None))
        )

        # Memory: inline MEMORY.md into the system prompt when enabled.
        system_prompt = handoff.agent.instructions or ""
        if self._agent_memory_enabled(resolved_name):
            memory_dir = get_memory_dir(umo, resolved_name)
            system_prompt = (
                f"{system_prompt}\n\n# Persistent Memory\n"
                f"Your memory directory is: {memory_dir}\n"
                "Use MEMORY.md as the concise index and split large topics into separate files."
            )
            memory_inline = load_memory_index_inline(umo, resolved_name)
            if memory_inline:
                system_prompt = f"{system_prompt}\n\n# Memory\n\n{memory_inline}"

        true_user_input = str(payload.get("true_user_input") or "")
        image_urls_raw = payload.get("image_urls_raw")
        provider_settings = self._load_provider_settings(umo)
        tool_call_timeout = self._safe_int_setting(
            provider_settings.get("tool_call_timeout", 60), 60
        )
        agent_max_step = self._safe_int_setting(
            provider_settings.get("max_agent_step", 30), 30
        )
        image_urls = await collect_child_image_urls(event, image_urls_raw)
        user_input_block = f"【对方原话】\n{true_user_input}\n\n" if true_user_input.strip() else ""
        maid_request_block = f"【大小姐请求】\n{request_text}\n\n"
        dispatch_prompt = render_dispatch_prompt(
            self.maid_mode_config.dispatch_prompt_template,
            user_input_block=user_input_block,
            maid_request_block=maid_request_block,
            maid_full_reply_block="",
        )
        trace_hooks = _RuntimeTraceHooks(
            run,
            self.runtime_store,
            lambda: self._publish_runtime_trace_safe(run),
        )

        runner_holder: dict[str, object] = {}

        async def _steer_handler(text: str) -> str:
            runner_obj = runner_holder.get("runner")
            if runner_obj is None:
                return ""
            ticket = runner_obj.follow_up(message_text=text)
            if ticket is None:
                raise RunNotFoundError("runner 已进入终态，无法接收 steer。")
            return str(ticket.seq)

        async def _run() -> str:
            from .maid_dispatcher import _build_runner as _build_runner_func

            runner = await _build_runner_func(
                context=context,
                event=child_event,
                provider=provider,
                prompt=dispatch_prompt,
                image_urls=image_urls,
                system_prompt=system_prompt,
                tools=toolset,
                contexts=initial_contexts,
                stream=bool(provider_settings.get("streaming_response", False)),
                tool_call_timeout=tool_call_timeout,
                llm_compress_instruction=str(provider_settings.get("llm_compress_instruction", "") or ""),
                llm_compress_keep_recent=self._safe_int_setting(
                    provider_settings.get("llm_compress_keep_recent", 4), 4
                ),
                llm_compress_provider=_get_compress_provider(context, provider_settings),
                truncate_turns=self._safe_int_setting(
                    provider_settings.get("dequeue_context_length", 1), 1
                ),
                enforce_max_turns=self._safe_int_setting(
                    provider_settings.get("max_context_length", -1), -1
                ),
                tool_schema_mode=str(provider_settings.get("tool_schema_mode", "full") or "full"),
                max_context_tokens=self._ensure_provider_max_context_tokens(provider),
                session_id=run.task_id,
                agent_hooks=trace_hooks,
            )
            runner_holder["runner"] = runner
            self.orchestrator.register_steer_handler(run.agent_id, _steer_handler)

            def _stop_handler() -> None:
                child_event.set_extra("agent_stop_requested", True)
                runner.request_stop()

            self.orchestrator.register_stop_handler(run.agent_id, _stop_handler)
            await self.runtime_store.append_control(
                run.agent_id, "run_start", {"task_id": run.task_id}
            )
            await self.runtime_store.append_message(
                run.agent_id,
                {"role": "user", "content": dispatch_prompt, "task_id": run.task_id},
            )
            try:
                step_count = 0
                persisted_count = len(getattr(runner.run_context, "messages", []) or [])
                while not runner.done() and step_count < agent_max_step:
                    step_count += 1
                    if child_event.get_extra("agent_stop_requested"):
                        runner.request_stop()
                    async for _ in runner.step():
                        persisted_count = await self._persist_runner_step(
                            run,
                            runner,
                            persisted_count,
                        )
                        if child_event.get_extra("agent_stop_requested"):
                            runner.request_stop()
                    persisted_count = await self._persist_runner_step(
                        run,
                        runner,
                        persisted_count,
                    )
                    await trace_hooks.close_unfinished_tool()
                if not runner.done():
                    if runner.req:
                        runner.req.func_tool = None
                    runner.run_context.messages.append(
                        _max_step_message(),
                    )
                    async for _ in runner.step():
                        persisted_count = await self._persist_runner_step(
                            run,
                            runner,
                            persisted_count,
                        )
                    await trace_hooks.close_unfinished_tool()
                llm_resp = runner.get_final_llm_resp()
                if llm_resp is None:
                    return ""
                return llm_resp.completion_text or ""
            finally:
                await trace_hooks.close_unfinished_tool()
                self.orchestrator.unregister_steer_handler(run.agent_id)
                await self.runtime_store.append_control(
                    run.agent_id, "run_end", {"task_id": run.task_id}
                )
                child_event.cleanup_temporary_local_files()

        runner_obj = _ChildRunner(_run)
        return runner_obj

    async def _persist_runner_step(
        self,
        run: RunMeta,
        runner,
        persisted_count: int,
    ) -> int:
        """Append new runner messages to the append-only transcript."""
        try:
            messages = getattr(runner, "run_context", None)
            msgs = getattr(messages, "messages", []) if messages else []
            for msg in msgs[persisted_count:]:
                dumped = msg.model_dump() if hasattr(msg, "model_dump") else msg
                await self.runtime_store.append_message(run.agent_id, dumped)
            if len(msgs) > persisted_count:
                await self._publish_runtime_trace_safe(run)
            return len(msgs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[大小姐模式] 追加 transcript 失败: %s", exc)
            return persisted_count

    async def _publish_runtime_trace_safe(self, run: RunMeta) -> None:
        try:
            records = await self.runtime_store.load_run_transcript(
                run.agent_id,
                run.task_id,
            )
            tool_chain = self._build_runtime_tool_chain_payload(records)
            current = await self.runtime_store.load_run(run.agent_id, run.task_id)
            await self.console_store.publish_runtime_trace(
                agent_id=run.agent_id,
                task_id=run.task_id,
                status=current.status if current is not None else run.status,
                tool_chain=tool_chain,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[大小姐模式] 发布 runtime 工具调用链失败: task_id=%s err=%s",
                run.task_id,
                exc,
            )

    def _isolate_child_event(self, event: AstrMessageEvent):
        """Return a child event sharing UMO/sender/role/group/platform but with
        isolated extras/result/stop/tempfile state."""
        return _IsolatedMaidEvent(event)

    def _resolve_handoff_for_runtime(self, agent_name: str):
        from .maid_dispatcher import _resolve_handoff

        return _resolve_handoff(
            self.context,
            agent_name,
            fallback_agent_name=self.maid_mode_config.default_agent_name,
        )

    def _load_provider_settings(self, umo: str) -> dict:
        return _load_provider_settings_fn(self.context, umo)

    @staticmethod
    def _safe_int_setting(value, default: int) -> int:
        return _safe_int(value, default)

    def _ensure_provider_max_context_tokens(self, provider) -> int:
        from .maid_dispatcher import _ensure_provider_max_context_tokens

        return _ensure_provider_max_context_tokens(provider)

    def _agent_memory_enabled(self, agent_name: str) -> bool:
        return _agent_memory_enabled_fn(
            self.maid_mode_config.memory_agent_names, agent_name
        )

    async def _notify_main_agent(self, notifications) -> NotifierResult:
        """Wake the main agent once for one UMO notification snapshot."""
        if not notifications:
            return NotifierResult(delivered=True)
        umo = notifications[0].unified_msg_origin
        if not umo or any(item.unified_msg_origin != umo for item in notifications):
            return NotifierResult(delivered=False, error="notification snapshot 的 UMO 不一致")
        try:
            from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession as _MS
            from astrbot.core.provider.entities import ProviderRequest
            from astrbot.core.tools.message_tools import SendMessageToUserTool

            ctx = self.context
            session = _MS.from_str(umo)
            summary_lines = ["[管家任务通知]"]
            for item in notifications:
                summary_lines.append(
                    f"- agent={item.agent_id[:8]} task={item.task_id[:8]} "
                    f"status={item.status}\n  {item.result or item.error or '(空)'}"
                )
            summary = "\n".join(summary_lines)
            notification_ids = [item.notification_id for item in notifications]
            extras = {
                MAID_NOTIFICATION_IDS_META_KEY: notification_ids,
                "background_task_results": [
                    {
                        "task_id": item.task_id,
                        "agent_id": item.agent_id,
                        "status": item.status,
                        "result": item.result,
                        "error": item.error,
                    }
                    for item in notifications
                ],
            }
            cron_event = CronMessageEvent(
                context=ctx,
                session=session,
                message=summary,
                extras=extras,
                message_type=session.message_type,
            )
            conversation_id = await ctx.conversation_manager.get_curr_conversation_id(umo)
            if not conversation_id:
                conversation_id = await ctx.conversation_manager.new_conversation(umo)
            conv = await ctx.conversation_manager.get_conversation(umo, conversation_id)
            if conv is None:
                return NotifierResult(delivered=False, error="无法读取或创建 conversation")
            req = ProviderRequest()
            req.conversation = conv
            req.contexts = json.loads(conv.history or "[]")
            req.prompt = summary
            req.system_prompt = (
                "A background subagent finished. Summarize the results for the user. "
                "Use send_message_to_user to deliver the useful result directly."
            )
            req.func_tool = ToolSet()
            send_tool = ctx.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
            if send_tool is not None:
                req.func_tool.add_tool(send_tool)
            provider_settings = self._load_provider_settings(umo)
            config = MainAgentBuildConfig(
                tool_call_timeout=self._safe_int_setting(
                    provider_settings.get("tool_call_timeout", 60),
                    60,
                ),
                streaming_response=False,
                provider_settings=provider_settings,
            )
            result = await build_main_agent(
                event=cron_event,
                plugin_context=ctx,
                config=config,
                req=req,
            )
            if result is None:
                return NotifierResult(delivered=False, error="build_main_agent 返回 None")
            runner = result.agent_runner
            async for _ in runner.step_until_done(30):
                pass
            llm_resp = runner.get_final_llm_resp()
            history_summary = summary
            if llm_resp is not None and getattr(llm_resp, "completion_text", ""):
                history_summary = f"{summary}\n\n主 Agent 处理结果：{llm_resp.completion_text}"
            await persist_agent_history(
                ctx.conversation_manager,
                event=cron_event,
                req=result.provider_request,
                summary_note=history_summary,
            )
            persisted = await ctx.conversation_manager.get_conversation(umo, conversation_id)
            history = json.loads(persisted.history or "[]") if persisted else []
            if history and isinstance(history[-1], dict):
                history[-1][MAID_NOTIFICATION_IDS_META_KEY] = notification_ids
                if len(notification_ids) == 1:
                    history[-1][MAID_NOTIFICATION_ID_META_KEY] = notification_ids[0]
                await ctx.conversation_manager.update_conversation(
                    umo,
                    conversation_id,
                    history=history,
                )
            return NotifierResult(delivered=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[大小姐模式] notification 唤醒主 agent 失败: tasks=%s err=%s",
                ",".join(item.task_id[:8] for item in notifications),
                exc,
                exc_info=True,
            )
            return NotifierResult(delivered=False, error=str(exc))

    async def _scan_history_for_dedupe(self, umo: str) -> list[dict]:
        """Return conversation history (OpenAI message dicts) for a UMO."""
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await self.context.conversation_manager.get_conversation(umo, cid)
            if conv is None:
                return []
            history = json.loads(conv.history or "[]")
            return history if isinstance(history, list) else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[大小姐模式] 读取会话历史失败: %s", exc)
            return []

    def _register_console_web_apis(self) -> None:
        prefix = f"/{PLUGIN_DATA_DIR_NAME}/console"
        routes = [
            (f"{prefix}/overview", self.console_overview, ["GET"], "Maid console overview"),
            (f"{prefix}/tasks", self.console_tasks, ["GET"], "Maid console task list"),
            (
                f"{prefix}/tasks/<task_id>",
                self.console_task_detail,
                ["GET"],
                "Maid console task detail",
            ),
            (
                f"{prefix}/tasks/<task_id>/update",
                self.console_update_task,
                ["POST"],
                "Update maid console task",
            ),
            (
                f"{prefix}/tasks/<task_id>/delete",
                self.console_delete_task,
                ["POST"],
                "Delete maid console task",
            ),
            (
                f"{prefix}/tasks/<task_id>/events",
                self.console_task_events,
                ["GET"],
                "Maid console task events",
            ),
            (f"{prefix}/stream", self.console_stream, ["GET"], "Maid console SSE stream"),
            (
                f"{prefix}/upload",
                self.console_upload,
                ["POST"],
                "Upload an image for a dashboard maid task",
            ),
            (
                f"{prefix}/actions/dispatch",
                self.console_dispatch,
                ["POST"],
                "Dispatch maid task from dashboard",
            ),
            (
                f"{prefix}/actions/steer",
                self.console_steer,
                ["POST"],
                "Steer maid task from dashboard",
            ),
            (
                f"{prefix}/actions/stop",
                self.console_stop,
                ["POST"],
                "Stop maid task from dashboard",
            ),
            (
                f"{prefix}/actions/done",
                self.console_done,
                ["POST"],
                "Close maid session from dashboard",
            ),
            (
                f"{prefix}/actions/rerun",
                self.console_rerun,
                ["POST"],
                "Rerun maid task from dashboard",
            ),
            (
                f"{prefix}/actions/resume",
                self.console_resume,
                ["POST"],
                "Resume a 1.3.0 runtime agent from dashboard",
            ),
            (
                f"{prefix}/actions/result",
                self.console_result,
                ["POST"],
                "Query/await a 1.3.0 runtime task result from dashboard",
            ),
            (f"{prefix}/export", self.console_export, ["GET"], "Export maid console history"),
            (f"{prefix}/clear", self.console_clear, ["POST"], "Clear maid console history"),
            (f"{prefix}/settings", self.console_settings_get, ["GET"], "Get maid settings"),
            (f"{prefix}/settings", self.console_settings_save, ["POST"], "Save maid settings"),
            (f"{prefix}/subagents", self.console_subagents, ["GET"], "List subagents"),
            (f"{prefix}/agents", self.console_agents, ["GET"], "List 1.3.0 runtime agents"),
            (
                f"{prefix}/agents/<agent_id>/runs",
                self.console_agent_runs,
                ["GET"],
                "List runs for a 1.3.0 runtime agent",
            ),
            (
                f"{prefix}/agents/<agent_id>/runs/<task_id>/trace",
                self.console_agent_run_trace,
                ["GET"],
                "Get one 1.3.0 runtime run trace",
            ),
            (
                f"{prefix}/agents/<agent_id>/delete",
                self.console_delete_agent,
                ["POST"],
                "Delete a terminal 1.3.0 runtime agent",
            ),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)

    @staticmethod
    def _console_ok(data: Any | None = None, message: str | None = None) -> QuartResponse:
        return jsonify(
            {
                "status": "ok",
                "message": message,
                "data": data if data is not None else {},
            }
        )

    @staticmethod
    def _console_error(message: str, status_code: int = 400) -> QuartResponse:
        response = jsonify({"status": "error", "message": message, "data": {}})
        response.status_code = status_code
        return response

    @staticmethod
    async def _console_json_body() -> dict[str, Any]:
        data = await request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _console_upload_dir() -> Path:
        return Path(get_astrbot_temp_path()) / PLUGIN_DATA_DIR_NAME / "console_uploads"

    @staticmethod
    def _detect_console_image_format(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp"
        return ""

    @classmethod
    async def _save_console_image_upload(cls, upload: Any) -> dict[str, Any]:
        declared_size = getattr(upload, "content_length", None)
        if isinstance(declared_size, int) and declared_size > _CONSOLE_IMAGE_MAX_BYTES:
            raise ValueError("图片不能超过 10 MB。")

        content_type = str(getattr(upload, "content_type", "") or "")
        content_type = content_type.split(";", 1)[0].strip().lower()
        allowed_mime_types = {
            mime_type
            for mime_types in _CONSOLE_IMAGE_MIME_TYPES.values()
            for mime_type in mime_types
        }
        if content_type not in allowed_mime_types:
            raise ValueError("仅支持 JPEG、PNG、WEBP 或 GIF 图片。")

        read_result = upload.read(_CONSOLE_IMAGE_MAX_BYTES + 1)
        data = await read_result if isawaitable(read_result) else read_result
        if not data:
            raise ValueError("上传的图片为空。")
        if len(data) > _CONSOLE_IMAGE_MAX_BYTES:
            raise ValueError("图片不能超过 10 MB。")

        image_format = cls._detect_console_image_format(data)
        if not image_format:
            raise ValueError("文件内容不是受支持的图片。")
        if content_type not in _CONSOLE_IMAGE_MIME_TYPES[image_format]:
            raise ValueError("图片 MIME 类型与文件内容不一致。")

        upload_dir = cls._console_upload_dir()
        await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
        extension = _CONSOLE_IMAGE_EXTENSIONS[image_format]
        destination = upload_dir / f"{uuid.uuid4().hex}{extension}"
        await asyncio.to_thread(destination.write_bytes, data)

        raw_name = str(getattr(upload, "filename", "") or "")
        basename = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        safe_name = "".join(char for char in basename if char.isprintable())[:120]
        if not safe_name:
            safe_name = f"image{extension}"
        return {
            "path": str(destination.resolve()),
            "name": safe_name,
            "mime_type": _CONSOLE_IMAGE_CANONICAL_MIME[image_format],
            "size": len(data),
        }

    @classmethod
    def _normalize_console_image_paths(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        raw_paths = [value] if isinstance(value, str) else value
        if not isinstance(raw_paths, list):
            raise ValueError("image_urls_raw 必须是图片路径数组。")
        if len(raw_paths) > _CONSOLE_IMAGE_MAX_COUNT:
            raise ValueError(f"每次最多发送 {_CONSOLE_IMAGE_MAX_COUNT} 张图片。")

        upload_dir = cls._console_upload_dir().resolve()
        normalized: list[str] = []
        allowed_extensions = set(_CONSOLE_IMAGE_EXTENSIONS.values())
        for raw_path in raw_paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("image_urls_raw 包含无效图片路径。")
            try:
                path = Path(raw_path).resolve(strict=True)
                path.relative_to(upload_dir)
            except (OSError, ValueError) as exc:
                raise ValueError("图片路径不是本 Console 上传的安全临时文件。") from exc
            if not path.is_file() or path.suffix.casefold() not in allowed_extensions:
                raise ValueError("图片临时文件无效。")
            if path.stat().st_size > _CONSOLE_IMAGE_MAX_BYTES:
                raise ValueError("图片不能超过 10 MB。")
            normalized_path = str(path)
            if normalized_path not in normalized:
                normalized.append(normalized_path)
        return normalized

    @staticmethod
    def _validate_dashboard_umo(unified_msg_origin: str) -> str:
        value = unified_msg_origin.strip()
        if not value:
            raise ValueError("需要填写 unified_msg_origin。")
        try:
            return str(MessageSession.from_str(value))
        except Exception as exc:
            raise ValueError(
                "unified_msg_origin 必须是 AstrBot 标准格式: platform:MessageType:session_id"
            ) from exc

    @staticmethod
    def _config_payload(config) -> dict[str, Any]:
        payload = asdict(config)
        for key in (
            "session_enabled",
            "session_timeout_minutes",
            "dispatch_auto_background_enabled",
            "dispatch_auto_background_seconds",
        ):
            payload.pop(key, None)
        return payload

    def _build_task_patch(
        self,
        *,
        task_id: str,
        kind: str,
        source: str,
        unified_msg_origin: str,
        sender_id: str,
        agent_name: str,
        status: str,
        request_text: str,
        parent_task_id: str = "",
        title: str = "",
        meta: dict[str, Any] | None = None,
    ) -> ConsoleTaskPatch:
        return ConsoleTaskPatch(
            task_id=task_id,
            kind=kind,
            source=source,
            unified_msg_origin=unified_msg_origin,
            sender_id=sender_id,
            agent_name=agent_name,
            status=status,
            title=title,
            request_text=request_text,
            parent_task_id=parent_task_id,
            meta=meta or {},
        )

    async def _console_ensure_task_safe(self, patch: ConsoleTaskPatch) -> None:
        try:
            await self.console_store.ensure_task(patch)
        except Exception as exc:
            logger.warning("[大小姐模式] 控制台任务记录失败: %s", exc)

    async def _console_update_status_safe(
        self,
        task_id: str,
        status: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.console_store.update_task_status(task_id, status, meta=meta)
        except Exception as exc:
            logger.warning("[大小姐模式] 控制台任务状态更新失败: %s", exc)

    async def _console_event_safe(
        self,
        *,
        task_id: str,
        event_type: str,
        title: str,
        message: str = "",
        source: str = "system",
        status: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.console_store.record_event(
                task_id=task_id,
                event_type=event_type,
                title=title,
                message=message,
                source=source,
                status=status,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("[大小姐模式] 控制台事件记录失败: %s", exc)

    async def _console_action_safe(
        self,
        *,
        task_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        result_text: str = "",
        status: str = "ok",
    ) -> None:
        try:
            await self.console_store.record_action(
                task_id=task_id,
                action=action,
                source="dashboard",
                payload=payload,
                result_text=result_text,
                status=status,
            )
        except Exception as exc:
            logger.warning("[大小姐模式] 控制台动作记录失败: %s", exc)

    @staticmethod
    def _message_content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                else:
                    text = getattr(item, "text", None) or getattr(item, "content", None)
                    if text:
                        parts.append(str(text))
            return "\n".join(parts)
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False, indent=2, default=str)
        return str(content)

    @staticmethod
    def _parse_tool_arguments(arguments: Any) -> Any:
        if not isinstance(arguments, str):
            return arguments
        value = arguments.strip()
        if not value:
            return ""
        try:
            return json.loads(value)
        except Exception:
            return arguments

    @staticmethod
    def _stringify_tool_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def _build_tool_chain_payload(cls, messages: list[dict[str, Any]]) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        raw_messages: list[dict[str, Any]] = []
        tool_names_by_id: dict[str, str] = {}

        for index, raw_message in enumerate(messages):
            message = raw_message if isinstance(raw_message, dict) else {"repr": repr(raw_message)}
            raw_messages.append(message)
            role = str(message.get("role") or "")
            content_text = cls._message_content_to_text(message.get("content"))

            if role == "assistant" and content_text.strip():
                entries.append(
                    {
                        "index": index,
                        "kind": "assistant",
                        "title": "子 agent 输出",
                        "message": content_text,
                    }
                )

            if role == "assistant":
                for raw_call in message.get("tool_calls") or []:
                    call = raw_call if isinstance(raw_call, dict) else {"repr": repr(raw_call)}
                    function = (
                        call.get("function") if isinstance(call.get("function"), dict) else {}
                    )
                    tool_call_id = str(call.get("id") or "")
                    tool_name = str(function.get("name") or "")
                    parsed_arguments = cls._parse_tool_arguments(function.get("arguments"))
                    if tool_call_id:
                        tool_names_by_id[tool_call_id] = tool_name
                    entries.append(
                        {
                            "index": index,
                            "kind": "tool_call",
                            "title": f"调用 {tool_name or '工具'}",
                            "message": cls._stringify_tool_value(parsed_arguments),
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "arguments": parsed_arguments,
                        }
                    )
                continue

            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "")
                tool_name = tool_names_by_id.get(tool_call_id, "")
                entries.append(
                    {
                        "index": index,
                        "kind": "tool_result",
                        "title": f"{tool_name or tool_call_id or '工具'} 返回",
                        "message": content_text,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                    }
                )

        return {
            "entries": entries,
            "messages": raw_messages,
            "message_count": len(raw_messages),
        }

    @staticmethod
    def _runtime_tool_result_to_text(tool_result: Any) -> str:
        if tool_result is None:
            return "工具未返回内容。"
        parts: list[str] = []
        for item in getattr(tool_result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
                continue
            mime_type = getattr(item, "mimeType", None)
            if mime_type:
                parts.append(f"[{mime_type} 内容]")
        result = "\n\n".join(part for part in parts if part).strip()
        if not result:
            result = str(tool_result)
        if bool(getattr(tool_result, "isError", False)) and not result.lower().startswith("error"):
            result = f"error: {result}"
        return result

    @classmethod
    def _build_runtime_tool_chain_payload(
        cls,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        messages = [record for record in records if not record.get("_control")]
        has_tool_controls = any(
            record.get("_control") and record.get("kind") == "tool_start"
            for record in records
        )
        if not has_tool_controls:
            return cls._build_tool_chain_payload(messages)

        entries: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if record.get("_control"):
                kind = str(record.get("kind") or "")
                if kind == "tool_start":
                    entries.append(
                        {
                            "index": index,
                            "kind": "tool_call",
                            "title": f"调用 {record.get('tool_name') or '工具'}",
                            "message": cls._stringify_tool_value(record.get("arguments")),
                            "tool_name": str(record.get("tool_name") or ""),
                            "tool_call_id": str(record.get("tool_call_id") or ""),
                            "arguments": record.get("arguments"),
                        }
                    )
                elif kind == "tool_end":
                    entries.append(
                        {
                            "index": index,
                            "kind": "tool_result",
                            "title": f"{record.get('tool_name') or '工具'} 返回",
                            "message": str(record.get("result") or ""),
                            "tool_name": str(record.get("tool_name") or ""),
                            "tool_call_id": str(record.get("tool_call_id") or ""),
                        }
                    )
                continue
            if str(record.get("role") or "") != "assistant":
                continue
            content_text = cls._message_content_to_text(record.get("content"))
            if content_text.strip():
                entries.append(
                    {
                        "index": index,
                        "kind": "assistant",
                        "title": "子 agent 输出",
                        "message": content_text,
                    }
                )
        return {
            "entries": entries,
            "messages": messages,
            "message_count": len(messages),
        }

    async def _console_tool_chain_snapshot_safe(
        self,
        task_id: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not task_id:
            return
        try:
            payload = self._build_tool_chain_payload(messages)
            if meta:
                payload.update(meta)
            await self.console_store.update_task_meta(
                task_id,
                meta_update={"tool_chain": payload},
            )
        except Exception as exc:
            logger.warning("[大小姐模式] 控制台工具调用链记录失败: %s", exc)

    async def _update_single_assistant_output(
        self,
        task_id: str,
        output: str,
    ):
        task_info = await self.background_tasks.update_assistant_output(task_id, output)
        if task_info is not None:
            await self._console_event_safe(
                task_id=task_id,
                event_type="agent_output",
                title="管家输出更新",
                message=output,
                source="agent",
                status=task_info.status,
            )
        return task_info

    async def _update_batch_item_assistant_output(
        self,
        batch_id: str,
        item_id: str,
        output: str,
    ):
        item = await self.batch_registry.update_item_assistant_output(batch_id, item_id, output)
        if item is not None:
            await self._console_event_safe(
                task_id=item_id,
                event_type="agent_output",
                title="批量子任务输出更新",
                message=output,
                source="agent",
                status=item.status,
                payload={"batch_id": batch_id},
            )
        return item

    def _create_dashboard_event(
        self,
        *,
        unified_msg_origin: str,
        sender_id: str = "dashboard",
        message_text: str = "",
    ) -> _DashboardMaidEvent:
        return _DashboardMaidEvent(
            unified_msg_origin=self._validate_dashboard_umo(unified_msg_origin),
            sender_id=sender_id,
            message_text=message_text,
        )

    @staticmethod
    def _active_task_payload(task_info) -> dict[str, Any]:
        return asdict(task_info)

    @staticmethod
    def _batch_payload(batch) -> dict[str, Any]:
        data = asdict(batch)
        data["item_statuses"] = {
            "queued": sum(item["status"] == "queued" for item in data["items"]),
            "running": sum(item["status"] == "running" for item in data["items"]),
            "done": sum(item["status"] == "done" for item in data["items"]),
            "error": sum(item["status"] == "error" for item in data["items"]),
            "stopped": sum(item["status"] == "stopped" for item in data["items"]),
        }
        return data

    async def console_overview(self):
        overview = await self.console_store.get_overview()
        active_tasks = await self.background_tasks.list_active()
        active_payload = [self._active_task_payload(task) for task in active_tasks]
        batches = []
        for task in active_tasks:
            if task.kind != "batch":
                continue
            batch = await self.batch_registry.get_batch(task.task_id)
            if batch is not None:
                batches.append(self._batch_payload(batch))
        overview.update(
            {
                "active_tasks": active_payload,
                "active_batches": batches,
                "config": self._config_payload(self.maid_mode_config),
            }
        )
        return self._console_ok(overview)

    async def console_tasks(self):
        limit = request.args.get("limit", 80, type=int)
        status = request.args.get("status", "", type=str)
        query = request.args.get("query", "", type=str) or request.args.get("q", "", type=str)
        tasks = await self.console_store.list_tasks(limit=limit, status=status, query=query)
        return self._console_ok({"tasks": tasks})

    async def console_task_detail(self, task_id: str):
        task = await self.console_store.get_task(task_id)
        if task is None:
            return self._console_error("任务不存在。", status_code=404)
        events = await self.console_store.get_task_events(task_id)
        actions = await self.console_store.get_task_actions(task_id)
        batch = None
        if task.get("kind") == "batch":
            batch_info = await self.batch_registry.get_batch(task_id)
            batch = self._batch_payload(batch_info) if batch_info is not None else None
        return self._console_ok(
            {
                "task": task,
                "events": events,
                "actions": actions,
                "batch": batch,
            }
        )

    async def console_task_events(self, task_id: str):
        task = await self.console_store.get_task(task_id)
        if task is None:
            return self._console_error("任务不存在。", status_code=404)
        events = await self.console_store.get_task_events(task_id)
        actions = await self.console_store.get_task_actions(task_id)
        return self._console_ok({"events": events, "actions": actions})

    async def console_update_task(self, task_id: str):
        body = await self._console_json_body()
        title = body.get("title")
        meta = body.get("meta")
        task = await self.console_store.update_task_meta(task_id, title=title, meta_update=meta)
        if task is None:
            return self._console_error("任务不存在。", status_code=404)
        return self._console_ok({"task": task})

    async def console_delete_task(self, task_id: str):
        await self.console_store.delete_task(task_id)
        return self._console_ok({}, "任务已删除")

    async def console_stream(self):
        queue = await self.console_store.subscribe()

        async def stream():
            try:
                yield ": maid-console connected\n\n"
                while True:
                    item = await queue.get()
                    payload = json.dumps(item, ensure_ascii=False, default=str)
                    event_id = str(item.get("event_id") or uuid.uuid4().hex)
                    yield f"id: {event_id}\ndata: {payload}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                await self.console_store.unsubscribe(queue)

        response = await make_response(
            stream(),
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Transfer-Encoding": "chunked",
            },
        )
        response.timeout = None  # type: ignore[attr-defined]
        return response

    async def console_upload(self):
        uploaded = None
        try:
            files_result = request.files
            files = await files_result if isawaitable(files_result) else files_result
            uploaded = files.get("file")
            if uploaded is None:
                return self._console_error("需要 multipart 图片字段 file。")
            payload = await self._save_console_image_upload(uploaded)
            return self._console_ok(payload, "图片已上传。")
        except ValueError as exc:
            return self._console_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error("[大小姐模式] Console 图片上传失败: %s", exc, exc_info=True)
            return self._console_error("图片上传失败。", status_code=500)
        finally:
            if uploaded is not None:
                with suppress(Exception):
                    close_result = uploaded.close()
                    if isawaitable(close_result):
                        await close_result

    async def _start_dashboard_dispatch_task(
        self,
        *,
        unified_msg_origin: str,
        agent_name: str,
        request_text: str,
        maid_full_reply: str = "",
        true_user_input: str = "",
        rerun_of: str = "",
        parent_task_id: str = "",
    ) -> dict[str, Any]:
        unified_msg_origin = self._validate_dashboard_umo(unified_msg_origin)
        if not request_text.strip():
            raise ValueError("任务要求不能为空。")
        current = await self.background_tasks.get_active_by_umo(unified_msg_origin)
        if current is not None:
            raise ValueError(f"该会话已有活跃后台任务: {current.task_id}")

        resolved_agent_name = self._resolve_allowed_agent_name(
            self.maid_mode_config.allowed_agent_names,
            self.maid_mode_config.default_agent_name,
            agent_name,
        )
        event = self._create_dashboard_event(
            unified_msg_origin=unified_msg_origin,
            message_text=request_text,
        )
        pending = {
            "agent_name": resolved_agent_name,
            "maid_request": request_text,
            "maid_full_reply": maid_full_reply or "Dashboard 手动派发任务。",
            "true_user_input": true_user_input or request_text,
            "parent_message_id": str(event.message_obj.message_id or ""),
            "parent_task_id": parent_task_id,
            "meta": {"rerun_of": rerun_of} if rerun_of else {},
        }
        task_id = await self._launch_maid_dispatch(
            source="dashboard",
            event=event,
            req=None,
            pending=pending,
            kind="single",
        )
        await self.console_store.record_action(
            task_id=task_id,
            action="dispatch",
            source="dashboard",
            payload={
                "unified_msg_origin": unified_msg_origin,
                "agent_name": resolved_agent_name,
                "request_text": request_text,
                "rerun_of": rerun_of,
            },
            result_text="已从 dashboard 派发后台任务。",
        )
        task = await self.console_store.get_task(task_id)
        if task is None:
            active_task = await self.background_tasks.get_active_by_umo(unified_msg_origin)
            if active_task is not None and active_task.task_id == task_id:
                return self._active_task_payload(active_task)
            raise RuntimeError(f"Dashboard 任务启动后无法读取状态: {task_id}")
        return task

    async def console_dispatch(self):
        try:
            body = await self._console_json_body()
            umo = self._validate_dashboard_umo(str(body.get("unified_msg_origin") or ""))
            request_text = str(body.get("request_text") or "").strip()
            if not request_text:
                return self._console_error("需要 request_text。")
            image_urls_raw = self._normalize_console_image_paths(body.get("image_urls_raw"))
            agent_name = self._resolve_allowed_agent_name(
                self.maid_mode_config.allowed_agent_names,
                self.maid_mode_config.default_agent_name,
                str(body.get("agent_name") or ""),
            )
            event = self._create_dashboard_event(
                unified_msg_origin=umo,
                sender_id=str(body.get("sender_id") or "dashboard"),
                message_text=request_text,
            )
            outcome = await self.orchestrator.dispatch_single(
                event=event,
                request=DispatchRequest(
                    request_text=request_text,
                    agent_name=agent_name,
                    run_in_background=bool(body.get("run_in_background", True)),
                ),
                runner_payload={
                    "true_user_input": request_text,
                    "image_urls_raw": image_urls_raw,
                },
            )
            await self._console_ensure_task_safe(
                self._build_task_patch(
                    task_id=outcome.task_id,
                    kind="single",
                    source="dashboard",
                    unified_msg_origin=umo,
                    sender_id=event.get_sender_id(),
                    agent_name=outcome.agent_name,
                    status=outcome.status,
                    request_text=request_text,
                    meta={
                        "agent_id": outcome.agent_id,
                        "run_mode": outcome.mode,
                        "image_count": len(image_urls_raw),
                    },
                )
            )
            return self._console_ok({"outcome": outcome.to_dict()}, "已派发。")
        except Exception as exc:
            return self._console_error(str(exc))

    async def console_steer(self):
        body = await self._console_json_body()
        agent_id = str(body.get("agent_id") or "").strip()
        task_id = str(body.get("task_id") or "").strip()
        message_text = str(body.get("message_text") or "").strip()
        if not message_text or (not agent_id and not task_id):
            return self._console_error("需要 agent_id（或 task_id）和 message_text。")
        if not agent_id:
            agent_id = await self._resolve_agent_id_from_task(task_id)
        if not agent_id:
            return self._console_error("目标 agent 不存在。", status_code=404)
        try:
            ticket = await self.orchestrator.steer(
                agent_id=agent_id,
                message_text=message_text,
                trusted_internal=True,
            )
        except Exception as exc:
            return self._console_error(str(exc))
        result_text = "已将补充要求转交给运行中的 agent。"
        await self._console_action_safe(
            task_id=task_id or "__agent__",
            action="steer",
            payload={"agent_id": agent_id, "message_text": message_text},
            result_text=result_text,
        )
        return self._console_ok({"agent_id": agent_id, "ticket": ticket}, result_text)

    async def console_stop(self):
        body = await self._console_json_body()
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return self._console_error("需要 task_id。")
        try:
            outcome = await self.orchestrator.stop(
                task_id=task_id,
                trusted_internal=True,
            )
        except Exception as exc:
            return self._console_error(str(exc))
        await self._console_action_safe(
            task_id=task_id,
            action="stop",
            result_text="已请求停止。",
        )
        return self._console_ok({"outcome": outcome.to_dict()}, "已请求停止。")

    async def console_done(self):
        return self._console_ok({}, "done 已弃用；1.3 runtime 无隐式 session。")

    async def console_rerun(self):
        try:
            body = await self._console_json_body()
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                return self._console_error("需要 task_id。")
            found = await self.runtime_store.find_run(task_id)
            if found is None:
                return self._console_error("任务不存在。", status_code=404)
            _agent, run = found
            event = self._create_dashboard_event(
                unified_msg_origin=run.unified_msg_origin,
                sender_id=run.sender_id,
                message_text=run.request_text,
            )
            outcome = await self.orchestrator.dispatch_single(
                event=event,
                request=DispatchRequest(
                    request_text=run.request_text,
                    agent_name=run.agent_name,
                    run_in_background=True,
                ),
                runner_payload={"true_user_input": run.request_text, "image_urls_raw": None},
            )
            return self._console_ok({"outcome": outcome.to_dict()}, "已重新派发。")
        except Exception as exc:
            return self._console_error(str(exc))

    async def console_export(self):
        payload = await self.console_store.export_history()
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        response = await make_response(
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Disposition": 'attachment; filename="maid-console-history.json"',
            },
        )
        return response

    async def console_clear(self):
        await self.console_store.clear_history()
        return self._console_ok({}, "历史记录已清空。")

    async def console_settings_get(self):
        return self._console_ok(
            {
                "config": self._config_payload(self.maid_mode_config),
                "raw_config": dict(self.config or {}),
            }
        )

    async def console_settings_save(self):
        body = await self._console_json_body()
        allowed_keys = {
            "default_agent_name",
            "allowed_agent_names",
            "hide_native_tools",
            "hide_transfer_tools",
            "include_raw_user_input",
            "log_raw_llm_io",
            "dispatch_prompt_template",
            "foreground_timeout_seconds",
            "memory_agent_names",
            "max_active_per_umo",
            "max_active_global",
            "retention_days",
        }
        next_config = dict(self.config or {})
        for key in allowed_keys:
            if key in body:
                next_config[key] = body[key]
        loaded = load_maid_mode_config(next_config)
        if hasattr(self.config, "save_config"):
            self.config.save_config(next_config)
        else:
            self.config.clear()
            self.config.update(next_config)
        self.maid_mode_config = loaded
        self.orchestrator.config = loaded
        self.runtime_store.config = loaded
        if self.session_store:
            self.session_store.config = loaded
        await self._console_action_safe(
            task_id="__settings__",
            action="settings_save",
            payload={"keys": sorted(key for key in allowed_keys if key in body)},
            result_text="配置已保存。",
        )
        return self._console_ok({"config": self._config_payload(loaded)}, "配置已保存。")

    async def console_subagents(self):
        root_config = self.context.get_config()
        subagent_config = root_config.get("subagent_orchestrator", {})
        configured_agents = []
        if isinstance(subagent_config, dict) and isinstance(subagent_config.get("agents"), list):
            configured_agents = [
                item for item in subagent_config["agents"] if isinstance(item, dict)
            ]
        orchestrator = getattr(self.context, "subagent_orchestrator", None)
        handoffs = getattr(orchestrator, "handoffs", None) or []
        runtime_names = {
            str(getattr(getattr(handoff, "agent", None), "name", "") or "")
            for handoff in handoffs
            if getattr(handoff, "agent", None) is not None
        }
        agents = []
        for item in configured_agents:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            agents.append(
                {
                    "name": name,
                    "enabled": bool(item.get("enabled", True)),
                    "provider_id": item.get("provider_id"),
                    "persona_id": item.get("persona_id"),
                    "runtime_loaded": name in runtime_names,
                    "tools": item.get("tools"),
                }
            )
        for name in sorted(runtime_names):
            if not any(agent["name"] == name for agent in agents):
                agents.append(
                    {
                        "name": name,
                        "enabled": True,
                        "provider_id": None,
                        "persona_id": None,
                        "runtime_loaded": True,
                        "tools": None,
                    }
                )
        return self._console_ok({"agents": agents})

    async def console_agents(self):
        umo = str(request.args.get("umo") or "")
        agents = []
        for agent_id in await self.runtime_store.list_agent_ids():
            meta = await self.runtime_store.load_agent(agent_id)
            if meta is None or (umo and meta.unified_msg_origin != umo):
                continue
            runs = await self.runtime_store.list_runs(agent_id)
            latest = runs[-1] if runs else None
            agents.append(
                {
                    **meta.to_dict(),
                    "run_count": len(runs),
                    "last_run_mode": latest.mode if latest else "",
                    "last_background_reason": latest.background_reason if latest else "",
                    "pending_notification": bool(
                        latest
                        and latest.notification is not None
                        and not latest.notification.delivered
                    ),
                    "notification_id": (
                        latest.notification.notification_id
                        if latest and latest.notification is not None
                        else ""
                    ),
                }
            )
        return self._console_ok({"agents": agents})

    async def console_agent_runs(self, agent_id: str):
        meta = await self.runtime_store.load_agent(agent_id)
        if meta is None:
            return self._console_error("agent 不存在。", status_code=404)
        runs = [run.to_dict() for run in await self.runtime_store.list_runs(agent_id)]
        return self._console_ok({"agent": meta.to_dict(), "runs": runs})

    async def console_agent_run_trace(self, agent_id: str, task_id: str):
        try:
            run = await self.runtime_store.load_run(agent_id, task_id)
            if run is None or run.agent_id != str(agent_id or "").strip().casefold():
                return self._console_error("run 不存在。", status_code=404)
            records = await self.runtime_store.load_run_transcript(agent_id, task_id)
            return self._console_ok(
                {
                    "agent_id": run.agent_id,
                    "task_id": run.task_id,
                    "status": run.status,
                    "tool_chain": self._build_runtime_tool_chain_payload(records),
                }
            )
        except ValueError as exc:
            return self._console_error(str(exc))

    async def console_delete_agent(self, agent_id: str):
        try:
            body = await self._console_json_body()
            normalized = str(agent_id or "").strip().casefold()
            confirmation = str(body.get("confirm_agent_id") or "").strip().casefold()
            if not normalized or confirmation != normalized:
                return self._console_error("删除确认无效，请重新确认 Agent ID。")
            removed_runs = await self.orchestrator.delete_agent(normalized)
            removed_audits = 0
            try:
                removed_audits = await self.console_store.delete_agent_tasks(normalized)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[大小姐模式] runtime agent 已删除，但清理 Console 审计记录失败: "
                    "agent_id=%s err=%s",
                    normalized,
                    exc,
                )
            return self._console_ok(
                {
                    "agent_id": normalized,
                    "removed_runs": removed_runs,
                    "removed_audits": removed_audits,
                },
                "Agent 及其所有 Run 已删除。",
            )
        except RunNotFoundError as exc:
            return self._console_error(str(exc), status_code=404)
        except (AgentBusyError, PendingNotificationError) as exc:
            return self._console_error(str(exc), status_code=409)
        except Exception as exc:
            return self._console_error(str(exc))

    async def console_resume(self):
        try:
            body = await self._console_json_body()
            agent_id = str(body.get("agent_id") or "").strip()
            request_text = str(body.get("request_text") or "").strip()
            umo = str(body.get("unified_msg_origin") or "").strip()
            if not agent_id or not request_text:
                return self._console_error("需要 agent_id 和 request_text。")
            image_urls_raw = self._normalize_console_image_paths(body.get("image_urls_raw"))
            meta = await self.runtime_store.load_agent(agent_id)
            if meta is None:
                return self._console_error("agent 不存在。", status_code=404)
            if umo and self._validate_dashboard_umo(umo) != meta.unified_msg_origin:
                return self._console_error("agent 不属于指定的 unified_msg_origin。")
            event = self._create_dashboard_event(
                unified_msg_origin=meta.unified_msg_origin,
                message_text=request_text,
                sender_id=meta.sender_id,
            )
            outcome = await self.orchestrator.dispatch_single(
                event=event,
                request=DispatchRequest(
                    request_text=request_text,
                    resume_agent_id=agent_id,
                ),
                runner_payload={
                    "true_user_input": request_text,
                    "image_urls_raw": image_urls_raw,
                },
            )
            return self._console_ok({"outcome": outcome.to_dict()}, "已发起 resume。")
        except Exception as exc:
            return self._console_error(str(exc))

    async def console_result(self):
        try:
            body = await self._console_json_body()
            agent_id = str(body.get("agent_id") or "").strip()
            task_id = str(body.get("task_id") or "").strip()
            block = bool(body.get("block", True))
            timeout_ms = int(body.get("timeout_ms", 30000))
            if not agent_id and not task_id:
                return self._console_error("需要 agent_id 或 task_id。")
            target_agent = agent_id or await self._resolve_agent_id_from_task(task_id)
            if not target_agent:
                return self._console_error(f"未找到 task_id={task_id} 对应的 agent。", status_code=404)
            outcome = await self.orchestrator.get_result(
                agent_id=target_agent,
                task_id=task_id,
                block=block,
                timeout_ms=timeout_ms,
                trusted_internal=True,
            )
            if outcome.status in {STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED}:
                await self.outbox.note_result_claimed(target_agent, task_id)
            return self._console_ok({"outcome": outcome.to_dict()})
        except RunNotFoundError as exc:
            return self._console_error(str(exc), status_code=404)
        except Exception as exc:
            return self._console_error(str(exc))

    async def _run_dashboard_dispatch_background_task(self, pending: dict[str, Any]) -> None:
        task_id = str(pending.get("task_id") or "")
        unified_msg_origin = str(pending.get("unified_msg_origin") or "")
        agent_name = str(pending.get("agent_name") or self.maid_mode_config.default_agent_name)
        maid_request = str(pending.get("maid_request") or "")
        maid_full_reply = str(pending.get("maid_full_reply") or "")
        true_user_input = str(pending.get("true_user_input") or "")
        agent_id = str(pending.get("agent_id") or task_id)
        parent_message_id = str(pending.get("parent_message_id") or "")
        event = self._create_dashboard_event(
            unified_msg_origin=unified_msg_origin,
            message_text=maid_request,
        )
        final_status = "done"
        dispatch_error = ""
        agent_result = ""
        resolved_agent_name = agent_name
        watchdog: asyncio.Task | None = None

        try:
            await self.background_tasks.mark_running(
                task_id,
                progress=f"Dashboard 管家任务开始执行: {maid_request[:80]}",
            )
            await self._console_update_status_safe(task_id, "running")
            await self._console_event_safe(
                task_id=task_id,
                event_type="running",
                title="管家开始执行",
                message=maid_request,
                source="dashboard",
                status="running",
                payload={"agent_name": agent_name},
            )
            watchdog = self._start_dispatch_watchdog(
                task_id=task_id,
                source="dashboard",
                event=event,
            )
            if self.session_store is None:
                raise RuntimeError("session_store 尚未初始化")
            agent_result, resolved_agent_name = await dispatch_to_maid_agent(
                context=self.context,
                event=event,
                session_store=self.session_store,
                agent_name=agent_name,
                maid_full_reply=maid_full_reply,
                maid_request=maid_request,
                true_user_input=true_user_input,
                image_urls_raw=None,
                on_runner_registered=self._register_background_runner,
                on_runner_unregistered=self._unregister_background_runner,
                on_assistant_output_updated=(
                    lambda output: self._update_single_assistant_output(task_id, output)
                ),
                on_tool_chain_updated=(
                    lambda messages: self._console_tool_chain_snapshot_safe(task_id, messages)
                ),
                agent_id=agent_id,
                parent_message_id=parent_message_id,
            )
            if event.get_extra("agent_stop_requested"):
                final_status = "stopped"
            await self._console_event_safe(
                task_id=task_id,
                event_type="agent_result",
                title="管家返回结果",
                message=agent_result,
                source="agent",
                status=final_status,
                payload={"agent_name": resolved_agent_name},
            )
            for sent_text in event.sent_messages:
                await self._console_event_safe(
                    task_id=task_id,
                    event_type="tool_direct_message",
                    title="工具请求发送消息",
                    message=sent_text,
                    source="tool",
                    status=final_status,
                )
        except asyncio.CancelledError:
            dispatch_error = "Dashboard 管家任务已取消。"
            final_status = "stopped"
            raise
        except Exception as exc:
            if event.get_extra("agent_stop_requested"):
                dispatch_error = "Dashboard 管家任务已停止。"
                final_status = "stopped"
                logger.info(
                    "[大小姐模式] Dashboard 管家任务已按请求停止: task_id=%s",
                    task_id,
                )
            else:
                dispatch_error = str(exc)
                final_status = "error"
                logger.error("[大小姐模式] Dashboard 管家任务失败: %s", exc, exc_info=True)
        finally:
            await self._cancel_dispatch_watchdog(watchdog)
            if self.session_store and final_status in {"stopped", "error"}:
                await self.session_store.close_active_session(
                    unified_msg_origin,
                    status=final_status,
                )
            await self.background_tasks.finish(
                task_id,
                status=final_status,
                result=agent_result,
                error=dispatch_error,
            )
            await self._console_ensure_task_safe(
                self._build_task_patch(
                    task_id=task_id,
                    kind="single",
                    source="dashboard",
                    unified_msg_origin=unified_msg_origin,
                    sender_id="dashboard",
                    agent_name=resolved_agent_name,
                    status=final_status,
                    request_text=maid_request,
                    title=f"手动派发: {maid_request[:60]}",
                    meta={"result": agent_result, "error": dispatch_error},
                )
            )
            await self._console_event_safe(
                task_id=task_id,
                event_type=(
                    "error"
                    if final_status == "error"
                    else ("stopped" if final_status == "stopped" else "finished")
                ),
                title=(
                    "Dashboard 任务失败"
                    if final_status == "error"
                    else (
                        "Dashboard 任务已停止"
                        if final_status == "stopped"
                        else "Dashboard 任务完成"
                    )
                ),
                message=dispatch_error or agent_result,
                source="system",
                status=final_status,
                payload={"agent_name": resolved_agent_name},
            )

    def _rewrite_response_text(self, resp: LLMResponse, text: str) -> None:
        """以兼容 AstrBot 的方式回写响应文本。"""
        resp.result_chain = MessageChain(chain=[Comp.Plain(text)])
        resp.completion_text = text
        resp.tools_call_name = []
        resp.tools_call_args = []
        resp.tools_call_ids = []
        resp.tools_call_extra_content = {}

    @staticmethod
    def _clear_response(resp: LLMResponse) -> None:
        resp.result_chain = None
        resp.completion_text = ""
        resp.tools_call_name = []
        resp.tools_call_args = []
        resp.tools_call_ids = []
        resp.tools_call_extra_content = {}

    @staticmethod
    def _contains_agent_name(agent_names: list[str] | None, agent_name: str) -> bool:
        if not agent_names:
            return False
        target = agent_name.strip().casefold()
        return any(name.strip().casefold() == target for name in agent_names)

    @staticmethod
    def _dump_json(data) -> str:
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return repr(data)

    @staticmethod
    def _is_provider_request_like(req: object) -> bool:
        return all(
            hasattr(req, attr)
            for attr in (
                "prompt",
                "image_urls",
                "contexts",
                "system_prompt",
                "model",
                "extra_user_content_parts",
            )
        )

    @staticmethod
    def _clear_pending_follow_up(event: AstrMessageEvent) -> None:
        event.set_extra(PENDING_MAID_FOLLOW_UP_EXTRA_KEY, None)

    @staticmethod
    def _extract_latest_assistant_text(event: AstrMessageEvent) -> str:
        result = event.get_result()
        if result is None:
            return ""
        try:
            return (result.get_plain_text() or "").strip()
        except Exception:
            return ""

    def _get_visible_tools_from_request(self, req: ProviderRequest) -> ToolSet:
        tool_set = ToolSet()
        source = req.func_tool
        if source is None:
            mgr = self.context.get_llm_tool_manager()
            source = mgr.get_full_tool_set()
        elif hasattr(source, "get_full_tool_set"):
            source = source.get_full_tool_set()

        for tool in getattr(source, "tools", []):
            if not getattr(tool, "active", True):
                continue
            tool_set.add_tool(tool)
        return tool_set

    def _build_main_model_toolset(self, req: ProviderRequest) -> ToolSet:
        mgr = self.context.get_llm_tool_manager()
        call_maid_tool = mgr.get_func(CALL_MAID_TOOL_NAME)
        maid_task_tool = mgr.get_func(MAID_TASK_TOOL_NAME)
        tool_set = ToolSet()
        if call_maid_tool is not None and getattr(call_maid_tool, "active", True):
            tool_set.add_tool(call_maid_tool)
        if maid_task_tool is not None and getattr(maid_task_tool, "active", True):
            tool_set.add_tool(maid_task_tool)

        if self.maid_mode_config.hide_native_tools:
            return tool_set

        for tool in self._get_visible_tools_from_request(req).tools:
            if self.maid_mode_config.hide_transfer_tools and tool.name.startswith("transfer_to_"):
                continue
            tool_set.add_tool(tool)
        return tool_set

    @staticmethod
    def _append_pending_dispatch(
        event: AstrMessageEvent,
        *,
        agent_name: str,
        maid_request: str,
        parent_message_id: str = "",
    ) -> int:
        pending = event.get_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY)
        items: list[dict[str, str]] = list(pending) if isinstance(pending, list) else []
        items.append(
            {
                "agent_name": agent_name,
                "maid_request": maid_request,
                "parent_message_id": parent_message_id,
            }
        )
        event.set_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY, items)
        return len(items)

    @staticmethod
    def _consume_pending_dispatches(event: AstrMessageEvent) -> list[dict[str, str]]:
        pending = event.get_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY)
        event.set_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY, None)
        if not isinstance(pending, list):
            return []
        return [
            {
                "agent_name": str(item.get("agent_name") or ""),
                "maid_request": str(item.get("maid_request") or ""),
                "parent_message_id": str(item.get("parent_message_id") or ""),
            }
            for item in pending
            if isinstance(item, dict) and str(item.get("maid_request") or "").strip()
        ]

    @staticmethod
    def _queue_call_maid_tool_history(
        event: AstrMessageEvent,
        *,
        action: str,
        request_text: str,
        agent_name: str,
        tool_result: str,
    ) -> None:
        pending = event.get_extra(PENDING_MAID_TOOL_HISTORY_EXTRA_KEY)
        items = list(pending) if isinstance(pending, list) else []
        items.append(
            {
                "action": action,
                "request_text": request_text,
                "agent_name": agent_name,
                "tool_result": tool_result,
            }
        )
        event.set_extra(PENDING_MAID_TOOL_HISTORY_EXTRA_KEY, items)

    @staticmethod
    def _consume_call_maid_tool_history(event: AstrMessageEvent) -> list[dict[str, str]]:
        pending = event.get_extra(PENDING_MAID_TOOL_HISTORY_EXTRA_KEY)
        event.set_extra(PENDING_MAID_TOOL_HISTORY_EXTRA_KEY, None)
        if not isinstance(pending, list):
            return []
        return [
            {
                "action": str(item.get("action") or ""),
                "request_text": str(item.get("request_text") or ""),
                "agent_name": str(item.get("agent_name") or ""),
                "tool_result": str(item.get("tool_result") or ""),
            }
            for item in pending
            if isinstance(item, dict)
        ]

    @staticmethod
    def _history_message_plain_text(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text") or "").strip()
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return ""

    @staticmethod
    def _build_call_maid_arguments(
        *,
        action: str,
        request_text: str,
        agent_name: str,
    ) -> dict[str, str]:
        arguments = {"action": action}
        if request_text.strip():
            arguments["request_text"] = request_text
        if agent_name.strip():
            arguments["agent_name"] = agent_name
        return arguments

    @classmethod
    def _build_call_maid_tool_result(
        cls,
        *,
        action: str,
        request_text: str,
        agent_name: str,
        tool_result: str,
        assistant_text: str = "",
        reasoning_content: str = "",
        reasoning_signature: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolCallsResult:
        actual_tool_call_id = tool_call_id or f"maid_hist_{uuid.uuid4().hex}"
        assistant_parts = [
            ThinkPart(
                think=reasoning_content or _EMPTY_REASONING_PLACEHOLDER,
                encrypted=reasoning_signature,
            )
        ]
        if assistant_text.strip():
            assistant_parts.append(TextPart(text=assistant_text))
        return ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                content=assistant_parts,
                tool_calls=[
                    ToolCall(
                        id=actual_tool_call_id,
                        function=ToolCall.FunctionBody(
                            name=CALL_MAID_TOOL_NAME,
                            arguments=json.dumps(
                                cls._build_call_maid_arguments(
                                    action=action,
                                    request_text=request_text,
                                    agent_name=agent_name,
                                ),
                                ensure_ascii=False,
                            ),
                        ),
                    )
                ],
            ),
            tool_calls_result=[
                ToolCallMessageSegment(
                    tool_call_id=actual_tool_call_id,
                    content=tool_result,
                )
            ],
        )

    async def _load_latest_conversation_history(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        *,
        purpose: str,
    ) -> list[dict] | None:
        snapshot: list[dict] | None = None
        try:
            parsed = json.loads(req.conversation.history or "[]")
            if isinstance(parsed, list):
                snapshot = parsed
        except Exception as exc:
            logger.warning(
                "[大小姐模式] 读取主对话历史快照失败，将从 conversation_manager 重新拉取 (%s): %s",
                purpose,
                exc,
            )

        try:
            curr_conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                req.conversation.cid,
            )
            if curr_conv is not None:
                latest = json.loads(curr_conv.history or "[]")
                if isinstance(latest, list):
                    return latest
        except Exception as exc:
            logger.warning(
                "[大小姐模式] 读取最新主对话历史失败，尝试使用请求快照 (%s): %s",
                purpose,
                exc,
            )

        if snapshot is not None:
            return snapshot
        logger.error("[大小姐模式] 无可用主对话历史，跳过回写: %s", purpose)
        return None

    async def _persist_assistant_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        reply_text: str,
        *,
        agent_id: str = "",
    ) -> None:
        if not req or not getattr(req, "conversation", None) or not reply_text.strip():
            return

        lock = self._conversation_history_locks.setdefault(
            event.unified_msg_origin,
            asyncio.Lock(),
        )
        async with lock:
            latest_history = await self._load_latest_conversation_history(
                event,
                req,
                purpose="写入后台回灌消息",
            )
            if latest_history is None:
                return
            assistant_message = {"role": "assistant", "content": reply_text}
            if agent_id:
                assistant_message[MAID_AGENT_ID_META_KEY] = agent_id
            latest_history.append(assistant_message)
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                req.conversation.cid,
                history=latest_history,
            )

    async def _persist_call_maid_tool_history(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        records: list[dict[str, str]],
        *,
        agent_id: str = "",
    ) -> None:
        if not req or not getattr(req, "conversation", None) or not records:
            return

        lock = self._conversation_history_locks.setdefault(
            event.unified_msg_origin,
            asyncio.Lock(),
        )
        async with lock:
            latest_history = await self._load_latest_conversation_history(
                event,
                req,
                purpose="写入 call_maid 工具记录",
            )
            if latest_history is None:
                return

            insertion_index = len(latest_history)
            anchor_found = False
            if agent_id:
                for idx in range(len(latest_history) - 1, -1, -1):
                    message = latest_history[idx]
                    if (
                        isinstance(message, dict)
                        and message.get("role") == "assistant"
                        and message.get(MAID_AGENT_ID_META_KEY) == agent_id
                    ):
                        insertion_index = idx
                        anchor_found = True
                        break
                if not anchor_found:
                    logger.warning(
                        "[大小姐模式] 未找到 agent_id 回灌锚点，将退回文本匹配: agent_id=%s",
                        agent_id,
                    )

            if not anchor_found:
                latest_assistant_text = self._extract_latest_assistant_text(event)
                if latest_assistant_text:
                    for idx in range(len(latest_history) - 1, -1, -1):
                        message = latest_history[idx]
                        if (
                            isinstance(message, dict)
                            and message.get("role") == "assistant"
                            and self._history_message_plain_text(message).strip()
                            == latest_assistant_text
                        ):
                            insertion_index = idx
                            break

            tool_history_messages: list[dict] = []
            for record in records:
                tool_history_messages.extend(
                    self._build_call_maid_tool_result(
                        action=str(record.get("action") or "").strip(),
                        request_text=str(record.get("request_text") or ""),
                        agent_name=str(record.get("agent_name") or ""),
                        tool_result=str(record.get("tool_result") or ""),
                    ).to_openai_messages()
                )

            latest_history[insertion_index:insertion_index] = tool_history_messages
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                req.conversation.cid,
                history=latest_history,
            )

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._active_asyncio_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._active_asyncio_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("[大小姐模式] 后台任务异常退出: %s", exc, exc_info=True)

        task.add_done_callback(_on_done)

    def _start_dispatch_watchdog(
        self,
        *,
        task_id: str,
        source: str,
        event: AstrMessageEvent,
    ) -> asyncio.Task | None:
        if not self.maid_mode_config.dispatch_auto_background_enabled:
            return None
        timeout_seconds = self.maid_mode_config.dispatch_auto_background_seconds
        if timeout_seconds <= 0:
            return None

        async def watch() -> None:
            await asyncio.sleep(timeout_seconds)
            current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
            if current is None or current.task_id != task_id:
                return
            await self._console_event_safe(
                task_id=task_id,
                event_type="auto_background",
                title="管家执行时间较长，仍在后台运行",
                message=f"任务已运行超过 {timeout_seconds} 秒，仍可使用 stop/steer 控制。",
                source="system",
                status=current.status,
                payload={"timeout_seconds": timeout_seconds},
            )
            if source == "chat":
                try:
                    await event.send(
                        MessageChain(chain=[Comp.Plain("管家还在忙哦，我先让它继续在后台处理～")])
                    )
                except Exception as exc:
                    logger.warning(
                        "[大小姐模式] 发送慢任务后台提示失败: task_id=%s error=%s",
                        task_id,
                        exc,
                    )

        return asyncio.create_task(watch(), name=f"maid-watchdog-{task_id[:8]}")

    @staticmethod
    async def _cancel_dispatch_watchdog(watchdog: asyncio.Task | None) -> None:
        if watchdog is None or watchdog.done():
            return
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog

    async def _launch_maid_dispatch(
        self,
        *,
        source: str,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
        pending: dict[str, Any],
        kind: str,
    ) -> str:
        if source not in {"chat", "dashboard"}:
            raise ValueError(f"不支持的 dispatch source: {source}")
        if kind not in {"single", "batch"}:
            raise ValueError(f"不支持的 dispatch kind: {kind}")
        if source == "dashboard" and kind != "single":
            raise ValueError("Dashboard 暂不支持批量 dispatch。")
        if source == "chat" and req is None:
            raise ValueError("聊天 dispatch 缺少 ProviderRequest。")

        if kind == "batch":
            items = pending.get("items")
            if not isinstance(items, list) or not items:
                raise ValueError("批量 dispatch 缺少任务条目。")
            agent_name = "batch"
            maid_request = self._summarize_batch_request(items)
            requested_task_id = str(pending.get("batch_id") or uuid.uuid4().hex)
            title = f"批量任务: {len(items)} 项"
            meta = {"item_count": len(items)}
        else:
            agent_name = str(pending.get("agent_name") or self.maid_mode_config.default_agent_name)
            maid_request = str(pending.get("maid_request") or "")
            requested_task_id = str(pending.get("task_id") or "") or None
            title_prefix = "手动派发" if source == "dashboard" else "管家任务"
            title = f"{title_prefix}: {maid_request[:60]}"
            meta = dict(pending.get("meta") or {})

        parent_message_id = str(
            pending.get("parent_message_id")
            or getattr(getattr(event, "message_obj", None), "message_id", "")
            or ""
        )
        sender_id = "dashboard" if source == "dashboard" else event.get_sender_id()
        task_info = await self.background_tasks.create_task(
            unified_msg_origin=event.unified_msg_origin,
            sender_id=sender_id,
            agent_name=agent_name,
            maid_request=maid_request,
            kind=kind,
            task_id=requested_task_id,
        )

        pending["agent_id"] = task_info.task_id
        pending["parent_message_id"] = parent_message_id
        pending["source"] = source
        pending["unified_msg_origin"] = event.unified_msg_origin
        if kind == "batch":
            pending["batch_id"] = task_info.task_id
        else:
            pending["task_id"] = task_info.task_id

        await self._console_ensure_task_safe(
            self._build_task_patch(
                task_id=task_info.task_id,
                kind=task_info.kind,
                source=source,
                unified_msg_origin=task_info.unified_msg_origin,
                sender_id=task_info.sender_id,
                agent_name=task_info.agent_name,
                status=task_info.status,
                request_text=task_info.maid_request,
                title=title,
                parent_task_id=str(pending.get("parent_task_id") or ""),
                meta={
                    **meta,
                    "agent_id": task_info.task_id,
                    "parent_message_id": parent_message_id,
                },
            )
        )
        await self._console_event_safe(
            task_id=task_info.task_id,
            event_type="queued",
            title=("批量任务已排队" if kind == "batch" else "管家任务已排队"),
            message=task_info.maid_request,
            source=source,
            status=task_info.status,
            payload={
                "agent_name": task_info.agent_name,
                "agent_id": task_info.task_id,
                "parent_message_id": parent_message_id,
                **meta,
            },
        )

        if source == "dashboard":
            coroutine = self._run_dashboard_dispatch_background_task(pending)
        elif kind == "batch":
            coroutine = self._run_maid_batch_background_task(event=event, req=req, pending=pending)
        else:
            coroutine = self._run_maid_follow_up_background_task(
                event=event,
                req=req,
                pending=pending,
            )
        runner_task = asyncio.create_task(
            coroutine,
            name=f"maid-{source}-{kind}-{task_info.task_id[:8]}",
        )
        self._track_background_task(runner_task)
        return task_info.task_id

    @staticmethod
    def _is_batch_pending(pending: object) -> bool:
        return isinstance(pending, dict) and pending.get("mode") == "batch"

    @staticmethod
    def _summarize_batch_request(items: list[dict]) -> str:
        return " | ".join(
            f"{str(item.get('agent_name') or '')}:{str(item.get('maid_request') or '')[:40]}"
            for item in items[:5]
        )

    @staticmethod
    def _join_batch_steer_text(items: list[dict]) -> str:
        parts: list[str] = []
        for index, item in enumerate(items, start=1):
            request = str(item.get("maid_request") or "").strip()
            agent_name = str(item.get("agent_name") or "").strip()
            if request:
                parts.append(f"{index}. {agent_name}: {request}")
        return "\n".join(parts).strip()

    @staticmethod
    def _resolve_allowed_agent_name(
        allowed_agent_names: list[str] | None,
        default_agent_name: str,
        requested_agent_name: str,
    ) -> str:
        agent_name = requested_agent_name or default_agent_name
        if allowed_agent_names and not MaidAgent._contains_agent_name(
            allowed_agent_names, agent_name
        ):
            logger.warning("[大小姐模式] call_maid 请求的目标 agent 不在白名单中: %s", agent_name)
            return default_agent_name
        return agent_name

    @staticmethod
    def _build_batch_follow_up_result(batch) -> str:
        lines = ["【批量管家结果汇总】"]
        success_count = 0
        failure_count = 0
        for index, item in enumerate(batch.items, start=1):
            if item.status == "done" and item.result.strip():
                success_count += 1
                lines.append(f"{index}. agent={item.agent_name}")
                lines.append(f"结果: {item.result.strip()}")
            elif item.status in {"error", "stopped"}:
                failure_count += 1
        if success_count == 0:
            lines.append("所有批量管家任务都未成功完成。")
        if failure_count > 0:
            lines.append(f"失败或停止的任务数: {failure_count}")
        return "\n".join(lines).strip()

    @filter.on_llm_request()
    async def sanitize_main_model_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """
        清洗主模型请求，实现大小姐模式。

        1. 保存原始对话输入到 event.extra
        2. 按配置重建主模型可见工具
        """
        if event.get_extra(MAID_NOTIFICATION_IDS_META_KEY):
            logger.debug("[大小姐模式] notification 唤醒请求保留专用 send_message_to_user 工具。")
            return
        raw_input = req.prompt or event.message_str or ""
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input)
            logger.debug("[大小姐模式] 已保存原始输入: %s...", raw_input[:100])

        true_user_input = event.message_str or ""
        if true_user_input:
            event.set_extra(TRUE_USER_INPUT_EXTRA_KEY, true_user_input)
            logger.debug("[大小姐模式] 已保存真实用户文本: %s...", true_user_input[:100])

        req.func_tool = self._build_main_model_toolset(req)
        logger.debug(
            "[大小姐模式] 已重建主模型工具集: %s",
            req.func_tool.names() if req.func_tool else [],
        )

        if self.maid_mode_config.log_raw_llm_io:
            logger.debug(
                "[大小姐模式] LLM请求原文:\n%s",
                self._dump_json(
                    {
                        "prompt": req.prompt,
                        "system_prompt": req.system_prompt,
                        "contexts": req.contexts,
                        "image_urls": req.image_urls,
                        "func_tool": (
                            [tool.name for tool in req.func_tool.tools]
                            if req.func_tool and getattr(req.func_tool, "tools", None)
                            else None
                        ),
                        "session_id": req.session_id,
                        "model": req.model,
                    }
                ),
            )

    @filter.llm_tool(name=CALL_MAID_TOOL_NAME)
    async def call_maid(
        self,
        event: AstrMessageEvent,
        request_text: str = "",
        agent_name: str = "",
        resume_agent_id: str = "",
        run_in_background: bool = False,
        tasks: list = None,
        action: str = "",
    ) -> str:
        """调度管家 subagent 执行任务，Claude Code 风格 foreground-first runtime。

        Args:
            request_text(string): 交给管家的任务要求。dispatch 必填；steer 时为补充要求。
            agent_name(string): 目标管家名称；留空使用默认管家。仅在新建 agent 时生效。
            resume_agent_id(string): 恢复已有 agent。填入则恢复该 agent；running 时作为 steer，终态时新建 task 后台执行。
            run_in_background(boolean): 默认 false 前台同步等待最多 foreground_timeout_seconds 秒；true 立即转后台。
            tasks(array): 批量任务列表，每项 {request_text, agent_name?, run_in_background?}，最多 5 项。仅新建 agent，不允许 resume。
            action(string): 1.2.0 旧接口兼容，可选 dispatch/steer/stop/done。使用新字段时留空。
        """
        # 1.2.0 legacy action compatibility.
        if action:
            return await self._call_maid_legacy_action(
                event, action, request_text, agent_name
            )

        true_user_input = str(event.get_extra(TRUE_USER_INPUT_EXTRA_KEY, "") or "")
        image_urls_raw = getattr(getattr(event, "message_obj", None), "image_urls", None)
        runner_payload = {
            "true_user_input": true_user_input,
            "image_urls_raw": image_urls_raw,
        }

        # Batch dispatch path.
        if tasks is not None:
            if resume_agent_id:
                return self._json_outcome(
                    {"status": "error", "error": "batch 仅允许创建新 agent，不能使用 resume_agent_id。"}
                )
            return await self._call_maid_batch(event, tasks, runner_payload)

        # Single dispatch.
        request_text = (request_text or "").strip()
        if not request_text:
            return self._json_outcome(
                {"status": "error", "error": "call_maid 需要提供非空的 request_text。"}
            )

        resolved = self._resolve_allowed_agent_name(
            self.maid_mode_config.allowed_agent_names,
            self.maid_mode_config.default_agent_name,
            agent_name,
        )
        request = DispatchRequest(
            request_text=request_text,
            agent_name=resolved,
            resume_agent_id=resume_agent_id,
            run_in_background=bool(run_in_background),
        )
        try:
            outcome = await self.orchestrator.dispatch_single(
                event=event,
                request=request,
                runner_payload=runner_payload,
            )
        except CapacityExceededError as exc:
            return self._json_outcome({"status": "error", "error": str(exc), "mode": "rejected"})
        except RunNotFoundError as exc:
            return self._json_outcome({"status": "error", "error": str(exc)})
        except PermissionError as exc:
            return self._json_outcome({"status": "error", "error": str(exc)})
        except (ValueError, AgentBusyError) as exc:
            return self._json_outcome({"status": "error", "error": str(exc)})

        current_run = await self.runtime_store.load_run(outcome.agent_id, outcome.task_id)
        audit_status = current_run.status if current_run is not None else outcome.status
        audit_mode = current_run.mode if current_run is not None else outcome.mode
        audit_reason = (
            current_run.background_reason
            if current_run is not None
            else outcome.background_reason
        )
        notification_id = (
            current_run.notification.notification_id
            if current_run is not None and current_run.notification is not None
            else ""
        )
        # Audit the 1.3.0 dispatch into SQLite; runtime files remain the truth.
        await self._console_ensure_task_safe(
            self._build_task_patch(
                task_id=outcome.task_id,
                kind="single",
                source="chat",
                unified_msg_origin=event.unified_msg_origin,
                sender_id=event.get_sender_id(),
                agent_name=outcome.agent_name,
                status=audit_status,
                request_text=request_text,
                title=f"管家任务: {request_text[:60]}",
                meta={
                    "agent_id": outcome.agent_id,
                    "run_mode": audit_mode,
                    "background_reason": audit_reason,
                    "notification_id": notification_id,
                },
            )
        )

        # Foreground completed inline -> deliver the result back as the tool
        # result so the main agent can use it in the same turn.
        if outcome.mode == "foreground" and outcome.status == STATUS_COMPLETED:
            await self.outbox.note_result_claimed(outcome.agent_id, outcome.task_id)
            return self._json_outcome(outcome.to_dict())
        # Foreground failure.
        if outcome.mode == "foreground" and outcome.status == STATUS_FAILED:
            await self.outbox.note_result_claimed(outcome.agent_id, outcome.task_id)
            return self._json_outcome(outcome.to_dict())
        # Background (explicit or migrated-on-timeout): return handle immediately.
        return self._json_outcome(outcome.to_dict())

    @staticmethod
    def _json_outcome(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def _call_maid_batch(self, event: AstrMessageEvent, tasks, runner_payload) -> str:
        if not isinstance(tasks, list) or not tasks:
            return self._json_outcome(
                {"status": "error", "error": "tasks 必须是非空列表。"}
            )
        if len(tasks) > 5:
            return self._json_outcome(
                {"status": "error", "error": f"batch 最多 5 项，收到 {len(tasks)}。"}
            )
        requests: list[DispatchRequest] = []
        for item in tasks:
            if not isinstance(item, dict):
                return self._json_outcome(
                    {"status": "error", "error": "batch 每项必须是对象。"}
                )
            if "resume_agent_id" in item:
                return self._json_outcome(
                    {"status": "error", "error": "batch 项不允许 resume_agent_id。"}
                )
            item_text = str(item.get("request_text") or "").strip()
            if not item_text:
                return self._json_outcome(
                    {"status": "error", "error": "batch 每项需要非空 request_text。"}
                )
            item_agent = str(item.get("agent_name") or "").strip()
            resolved = self._resolve_allowed_agent_name(
                self.maid_mode_config.allowed_agent_names,
                self.maid_mode_config.default_agent_name,
                item_agent,
            )
            requests.append(
                DispatchRequest(
                    request_text=item_text,
                    agent_name=resolved,
                    run_in_background=bool(item.get("run_in_background", False)),
                )
            )
        try:
            batch_outcome = await self.orchestrator.dispatch_batch(
                event=event,
                requests=requests,
                runner_payload=runner_payload,
            )
        except BatchCapacityError as exc:
            return self._json_outcome(
                {"status": "error", "error": f"批量容量不足，整批拒绝: {exc}", "mode": "rejected"}
            )
        except CapacityExceededError as exc:
            return self._json_outcome({"status": "error", "error": str(exc), "mode": "rejected"})
        for request_item, outcome in zip(requests, batch_outcome.items):
            current_run = await self.runtime_store.load_run(outcome.agent_id, outcome.task_id)
            status = current_run.status if current_run is not None else outcome.status
            mode = current_run.mode if current_run is not None else outcome.mode
            notification_id = (
                current_run.notification.notification_id
                if current_run is not None and current_run.notification is not None
                else ""
            )
            await self._console_ensure_task_safe(
                self._build_task_patch(
                    task_id=outcome.task_id,
                    kind="batch_item",
                    source="chat",
                    unified_msg_origin=event.unified_msg_origin,
                    sender_id=event.get_sender_id(),
                    agent_name=outcome.agent_name,
                    status=status,
                    request_text=request_item.request_text,
                    title=f"批量管家任务: {request_item.request_text[:60]}",
                    meta={
                        "batch_id": batch_outcome.batch_id,
                        "agent_id": outcome.agent_id,
                        "run_mode": mode,
                        "background_reason": outcome.background_reason,
                        "notification_id": notification_id,
                    },
                )
            )
            if outcome.mode == "foreground" and outcome.status in {
                STATUS_COMPLETED,
                STATUS_FAILED,
                STATUS_STOPPED,
            }:
                await self.outbox.note_result_claimed(outcome.agent_id, outcome.task_id)
        return self._json_outcome(batch_outcome.to_dict())

    async def _call_maid_legacy_action(
        self,
        event: AstrMessageEvent,
        action: str,
        request_text: str,
        agent_name: str,
    ) -> str:
        """1.2.0 旧 action 兼容路径。1.3.x 保留转换 + 弃用提示，1.4 移除。"""
        normalized = (action or "").strip().casefold()
        logger.warning(
            "[大小姐模式] call_maid 旧 action=%s 已弃用，请改用新接口（request_text/agent_name/resume_agent_id/run_in_background/tasks）。",
            normalized,
        )
        if normalized not in {"dispatch", "steer", "stop", "done"}:
            return "call_maid 的 action 非法（已弃用），请使用新接口。"
        if normalized == "stop":
            active = await self.orchestrator.list_active_runs(
                event.unified_msg_origin,
                event.get_sender_id(),
            )
            if active:
                outcomes = [
                    await self.orchestrator.stop(
                        task_id=run.task_id,
                        event=event,
                    )
                    for run in active
                ]
                return self._json_outcome(
                    {
                        "status": "deprecated",
                        "warning": "action=stop 已弃用，请改用 maid_task(stop, task_id)。",
                        "items": [item.to_dict() for item in outcomes],
                    }
                )
            return await self._request_stop_background_tasks(event)
        if normalized == "done":
            return "done 已弃用，1.3.0 runtime 无需显式结束 session。"
        if not request_text.strip():
            return "call_maid 需要提供非空的 request_text。"
        if normalized == "steer":
            active = await self.orchestrator.list_active_runs(
                event.unified_msg_origin,
                event.get_sender_id(),
            )
            if len(active) == 1:
                ticket = await self.orchestrator.steer(
                    agent_id=active[0].agent_id,
                    message_text=request_text,
                    event=event,
                )
                return self._json_outcome(
                    {
                        "status": "deprecated",
                        "warning": "action=steer 已弃用，请改用 maid_task(steer, agent_id)。",
                        "agent_id": active[0].agent_id,
                        "task_id": active[0].task_id,
                        "ticket": ticket,
                    }
                )
            if len(active) > 1:
                return "当前会话有多个活跃 agent；旧 action=steer 无法消歧，请使用 maid_task + agent_id。"
            return await self._steer_background_task(event, request_text)
        # dispatch via legacy: route to new single-dispatch (foreground).
        return await self.call_maid(
            event=event,
            request_text=request_text,
            agent_name=agent_name,
        )

    @filter.llm_tool(name=MAID_TASK_TOOL_NAME)
    async def maid_task(
        self,
        event: AstrMessageEvent,
        action: str,
        task_id: str = "",
        agent_id: str = "",
        message: str = "",
        block: bool = True,
        timeout_ms: int = 30000,
    ) -> str:
        """查询/控制管家 runtime 任务，对齐 Claude TaskOutput 语义。

        Args:
            action(string): 必填。可选 status/result/stop/steer。
                status 非阻塞查询任务状态；result 阻塞等待终态（默认 30 秒，最大 600 秒）；
                stop 停止任务；steer 向运行中的任务补充要求。
            task_id(string): status/result/stop 时填写的任务 ID。
            agent_id(string): steer 时填写的稳定 agent ID；result 可选填写用于交叉校验归属。
            message(string): steer 时的补充要求文本，必须非空。
            block(boolean): result 时是否阻塞等待。默认 true。
            timeout_ms(int): result 阻塞超时，毫秒。默认 30000，最大 600000。
        """
        normalized = (action or "").strip().casefold()
        if normalized == "steer":
            if not agent_id or not message.strip():
                return self._json_outcome(
                    {"status": "error", "error": "steer 需要 agent_id 和非空 message。"}
                )
            try:
                ticket = await self.orchestrator.steer(
                    agent_id=agent_id,
                    message_text=message,
                    event=event,
                )
            except RunNotFoundError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            except PermissionError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            return self._json_outcome(
                {"agent_id": agent_id, "status": "steered", "ticket": ticket}
            )
        if normalized == "stop":
            if not task_id:
                return self._json_outcome(
                    {"status": "error", "error": "stop 需要 task_id。"}
                )
            try:
                outcome = await self.orchestrator.stop(
                    task_id=task_id,
                    event=event,
                )
            except RunNotFoundError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            except PermissionError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            return self._json_outcome(outcome.to_dict())
        if normalized in {"status", "result"}:
            if not task_id:
                return self._json_outcome(
                    {"status": "error", "error": f"{normalized} 需要 task_id。"}
                )
            blocking = bool(block) if normalized == "result" else False
            try:
                outcome = await self.orchestrator.get_result(
                    task_id=task_id,
                    agent_id=agent_id,
                    event=event,
                    block=blocking,
                    timeout_ms=timeout_ms,
                )
            except RunNotFoundError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            except PermissionError as exc:
                return self._json_outcome({"status": "error", "error": str(exc)})
            # On successful terminal read, claim the pending notification so it
            # is not redelivered to the main agent.
            if outcome.status in {STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED}:
                await self.outbox.note_result_claimed(outcome.agent_id, task_id)
            return self._json_outcome(outcome.to_dict())
        return self._json_outcome(
            {"status": "error", "error": f"maid_task action 非法: {action}"}
        )

    async def _resolve_agent_id_from_task(self, task_id: str) -> str:
        """Reverse-lookup an agent_id from a task_id by scanning runtime store."""
        if not task_id:
            return ""
        found = await self.runtime_store.find_run(task_id)
        return found[0].agent_id if found is not None else ""


    async def _request_maid_follow_up(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        maid_visible_text: str,
        agent_name: str,
        maid_request: str,
        agent_result: str,
        reasoning_content: str = "",
        reasoning_signature: str | None = None,
    ) -> LLMResponse:
        _ = reasoning_signature
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到用于大小姐追答的 provider: {provider_id}")

        new_contexts = list(req.contexts)

        # Format assistant's previous message as pure text
        assistant_content = maid_visible_text or "雪雪去帮你查啦，等一下下哦～"
        if reasoning_content:
            assistant_content = f"<think>{reasoning_content}</think>\n{assistant_content}"

        new_contexts.append({"role": "assistant", "content": assistant_content})

        follow_up_prompt = f"[管家后台执行完成]\n执行管家: {agent_name}\n执行请求: {maid_request}\n执行结果:\n{agent_result}\n\n请向用户总结或汇报以上执行结果（请保持你原有的人设和说话风格）。"

        return await provider.text_chat(
            prompt=follow_up_prompt,
            image_urls=req.image_urls,
            func_tool=None,
            contexts=new_contexts,
            system_prompt=req.system_prompt,
            tool_calls_result=None,
            model=req.model,
            extra_user_content_parts=req.extra_user_content_parts,
        )

    async def _run_maid_follow_up_background_task(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        pending: dict,
    ) -> None:
        """执行单任务后台追答，并在停止/失败时主动收尾 active session。"""
        task_id = str(pending.get("task_id") or "")
        agent_name = pending.get("agent_name") or self.maid_mode_config.default_agent_name
        maid_full_reply = pending.get("maid_full_reply") or ""
        maid_request = pending.get("maid_request") or ""
        true_user_input = pending.get("true_user_input")
        image_urls_raw = pending.get("image_urls_raw")
        session_done_requested = bool(pending.get("session_done_requested", False))
        reasoning_content = str(pending.get("reasoning_content", "") or "")
        reasoning_signature = pending.get("reasoning_signature")
        agent_id = str(pending.get("agent_id") or task_id)
        parent_message_id = str(pending.get("parent_message_id") or "")
        source = str(pending.get("source") or "chat")
        dispatch_error: str | None = None
        final_status = "done"
        watchdog: asyncio.Task | None = None

        try:
            if task_id:
                await self.background_tasks.mark_running(
                    task_id,
                    progress=f"管家开始执行任务: {maid_request[:80]}",
                )
                await self._console_update_status_safe(task_id, "running")
                await self._console_event_safe(
                    task_id=task_id,
                    event_type="running",
                    title="管家开始执行",
                    message=maid_request,
                    source="runner",
                    status="running",
                    payload={"agent_name": agent_name},
                )
                watchdog = self._start_dispatch_watchdog(
                    task_id=task_id,
                    source=source,
                    event=event,
                )
            try:
                if self.session_store is None:
                    raise RuntimeError("session_store 尚未初始化")
                agent_result, resolved_agent_name = await dispatch_to_maid_agent(
                    context=self.context,
                    event=event,
                    session_store=self.session_store,
                    agent_name=agent_name,
                    maid_full_reply=maid_full_reply,
                    maid_request=maid_request,
                    true_user_input=true_user_input if isinstance(true_user_input, str) else None,
                    image_urls_raw=image_urls_raw,
                    on_runner_registered=self._register_background_runner,
                    on_runner_unregistered=self._unregister_background_runner,
                    on_assistant_output_updated=(
                        (lambda output: self._update_single_assistant_output(task_id, output))
                        if task_id
                        else None
                    ),
                    on_tool_chain_updated=(
                        (
                            lambda messages: self._console_tool_chain_snapshot_safe(
                                task_id,
                                messages,
                            )
                        )
                        if task_id
                        else None
                    ),
                    agent_id=agent_id,
                    parent_message_id=parent_message_id,
                )
                if task_id:
                    await self.background_tasks.update_progress(
                        task_id,
                        f"管家已完成执行，等待大小姐整理结果: {resolved_agent_name}",
                    )
                    await self._console_event_safe(
                        task_id=task_id,
                        event_type="agent_result",
                        title="管家返回结果",
                        message=agent_result,
                        source="agent",
                        status="running",
                        payload={"agent_name": resolved_agent_name},
                    )
            except Exception as exc:
                resolved_agent_name = agent_name
                if event.get_extra("agent_stop_requested"):
                    agent_result = "管家任务已按请求停止。"
                    logger.info(
                        "[大小姐模式] 子 agent 已按请求停止: task_id=%s",
                        task_id,
                    )
                else:
                    logger.error("[大小姐模式] 子 agent 调度失败: %s", exc, exc_info=True)
                    agent_result = f"执行过程中出现问题：{exc!s}"
                    dispatch_error = str(exc)
                    if task_id:
                        await self._console_event_safe(
                            task_id=task_id,
                            event_type="agent_error",
                            title="管家执行出错",
                            message=dispatch_error,
                            source="agent",
                            status="error",
                            payload={"agent_name": resolved_agent_name},
                        )

            maid_visible_text = maid_full_reply.strip()
            if task_id:
                await self._console_event_safe(
                    task_id=task_id,
                    event_type="follow_up_request",
                    title="请求大小姐整理结果",
                    message=agent_result,
                    source="system",
                    status="running",
                    payload={"agent_name": resolved_agent_name},
                )
            follow_up_resp = await self._request_maid_follow_up(
                event=event,
                req=req,
                maid_visible_text=maid_visible_text,
                agent_name=resolved_agent_name,
                maid_request=maid_request,
                agent_result=agent_result,
                reasoning_content=reasoning_content,
                reasoning_signature=(
                    str(reasoning_signature) if reasoning_signature is not None else None
                ),
            )
            follow_up_completion_text = follow_up_resp.completion_text or ""
            sanitized_follow_up = follow_up_completion_text.strip()
            if sanitized_follow_up != follow_up_completion_text and sanitized_follow_up:
                self._rewrite_response_text(follow_up_resp, sanitized_follow_up)

            if follow_up_resp.result_chain is not None or sanitized_follow_up.strip():
                chain = follow_up_resp.result_chain or MessageChain(
                    chain=[Comp.Plain(sanitized_follow_up)]
                )
                await event.send(chain)
                await self._persist_assistant_reply(
                    event,
                    req,
                    sanitized_follow_up or follow_up_completion_text,
                    agent_id=agent_id,
                )
                await self._persist_call_maid_tool_history(
                    event,
                    req,
                    [
                        {
                            "action": "dispatch",
                            "request_text": maid_request,
                            "agent_name": resolved_agent_name,
                            "tool_result": agent_result,
                        }
                    ],
                    agent_id=agent_id,
                )
                if task_id:
                    await self._console_event_safe(
                        task_id=task_id,
                        event_type="follow_up_sent",
                        title="大小姐追答已发送",
                        message=sanitized_follow_up or follow_up_completion_text,
                        source="chat",
                        status="running",
                    )

            final_status = (
                "stopped"
                if event.get_extra("agent_stop_requested")
                else ("error" if dispatch_error else "done")
            )
            if self.session_store and (
                session_done_requested or final_status in {"stopped", "error"}
            ):
                await self.session_store.close_active_session(
                    event.unified_msg_origin,
                    status=("done" if session_done_requested else final_status),
                )
            if task_id:
                await self.background_tasks.finish(
                    task_id,
                    status=final_status,
                    result=sanitized_follow_up or agent_result,
                    error=dispatch_error or "",
                )
                await self._console_update_status_safe(
                    task_id,
                    final_status,
                    meta={
                        "agent_name": resolved_agent_name,
                        "agent_result": agent_result,
                        "follow_up": sanitized_follow_up or follow_up_completion_text,
                        "error": dispatch_error or "",
                    },
                )
                await self._console_event_safe(
                    task_id=task_id,
                    event_type=("error" if final_status == "error" else "finished"),
                    title=("后台任务失败" if final_status == "error" else "后台任务完成"),
                    message=dispatch_error or sanitized_follow_up or agent_result,
                    source="system",
                    status=final_status,
                    payload={"agent_name": resolved_agent_name},
                )
        except asyncio.CancelledError:
            final_status = "stopped"
            if self.session_store:
                await self.session_store.close_active_session(
                    event.unified_msg_origin,
                    status=final_status,
                )
            if task_id:
                await self.background_tasks.finish(
                    task_id,
                    status=final_status,
                    error="后台追答任务已取消。",
                )
                await self._console_update_status_safe(
                    task_id,
                    final_status,
                    meta={"error": "后台追答任务已取消。"},
                )
            raise
        except Exception as exc:
            final_status = "stopped" if event.get_extra("agent_stop_requested") else "error"
            if self.session_store:
                await self.session_store.close_active_session(
                    event.unified_msg_origin,
                    status=final_status,
                )
            if task_id:
                await self.background_tasks.finish(
                    task_id,
                    status=final_status,
                    error=str(exc),
                )
                await self._console_update_status_safe(
                    task_id,
                    final_status,
                    meta={"error": str(exc)},
                )
                await self._console_event_safe(
                    task_id=task_id,
                    event_type=("stopped" if final_status == "stopped" else "error"),
                    title=(
                        "后台追答任务已停止" if final_status == "stopped" else "后台追答任务失败"
                    ),
                    message=str(exc),
                    source="system",
                    status=final_status,
                )
            if final_status == "stopped":
                logger.info("[大小姐模式] 后台追答任务已按请求停止: task_id=%s", task_id)
            else:
                logger.error("[大小姐模式] 后台追答任务失败: %s", exc, exc_info=True)
        finally:
            await self._cancel_dispatch_watchdog(watchdog)

    async def _run_maid_batch_item_background_task(
        self,
        *,
        event: AstrMessageEvent,
        batch_id: str,
        item_id: str,
        session_id: str,
        maid_full_reply: str,
        agent_name: str,
        maid_request: str,
        true_user_input: str | None,
        image_urls_raw: object,
        parent_message_id: str,
    ) -> None:
        """执行 batch 子任务；批量停止依赖 batch_registry 与 runner.request_stop。"""
        dispatch_error: str | None = None
        resolved_agent_name = agent_name
        result_text = ""
        final_status = "done"

        batch = await self.batch_registry.get_batch(batch_id)
        if batch is None:
            return
        if batch.stop_requested or event.get_extra("agent_stop_requested"):
            final_status = "stopped"
            await self.batch_registry.finish_item(
                batch_id,
                item_id,
                status=final_status,
                error="已请求停止",
                agent_name=resolved_agent_name,
            )
            await self._console_update_status_safe(item_id, final_status)
            await self._console_event_safe(
                task_id=item_id,
                event_type="stopped",
                title="批量子任务已停止",
                message="已请求停止",
                source="system",
                status=final_status,
                payload={"batch_id": batch_id, "agent_name": resolved_agent_name},
            )
            if self.session_store:
                await self.session_store.close_session(session_id, status=final_status)
            return

        await self.batch_registry.update_item_running(batch_id, item_id)
        await self._console_update_status_safe(item_id, "running")
        await self._console_event_safe(
            task_id=item_id,
            event_type="running",
            title="批量子任务开始执行",
            message=maid_request,
            source="runner",
            status="running",
            payload={"batch_id": batch_id, "agent_name": agent_name},
        )
        try:
            if self.session_store is None:
                raise RuntimeError("session_store 尚未初始化")
            result_text, resolved_agent_name = await dispatch_to_maid_agent(
                context=self.context,
                event=event,
                session_store=self.session_store,
                agent_name=agent_name,
                maid_full_reply=maid_full_reply,
                maid_request=maid_request,
                true_user_input=true_user_input,
                image_urls_raw=image_urls_raw,
                explicit_session_id=session_id,
                on_runner_registered=(
                    lambda _umo, runner: self._register_batch_runner(batch_id, runner)
                ),
                on_runner_unregistered=(
                    lambda _umo, runner: self._unregister_batch_runner(batch_id, runner)
                ),
                on_assistant_output_updated=(
                    lambda output: self._update_batch_item_assistant_output(
                        batch_id, item_id, output
                    )
                ),
                on_tool_chain_updated=(
                    lambda messages: self._console_tool_chain_snapshot_safe(
                        item_id,
                        messages,
                        meta={"batch_id": batch_id},
                    )
                ),
                agent_id=item_id,
                parent_message_id=parent_message_id,
            )
            refreshed_batch = await self.batch_registry.get_batch(batch_id)
            if refreshed_batch is not None and (
                refreshed_batch.stop_requested or event.get_extra("agent_stop_requested")
            ):
                final_status = "stopped"
            await self.batch_registry.finish_item(
                batch_id,
                item_id,
                status=final_status,
                result=result_text,
                agent_name=resolved_agent_name,
            )
            await self._console_update_status_safe(
                item_id,
                final_status,
                meta={
                    "batch_id": batch_id,
                    "agent_name": resolved_agent_name,
                    "agent_result": result_text,
                },
            )
            await self._console_event_safe(
                task_id=item_id,
                event_type=("stopped" if final_status == "stopped" else "finished"),
                title=("批量子任务已停止" if final_status == "stopped" else "批量子任务完成"),
                message=result_text,
                source="agent",
                status=final_status,
                payload={"batch_id": batch_id, "agent_name": resolved_agent_name},
            )
        except asyncio.CancelledError:
            final_status = "stopped"
            await self.batch_registry.finish_item(
                batch_id,
                item_id,
                status=final_status,
                error="批量子任务已取消。",
                agent_name=resolved_agent_name,
            )
            await self._console_update_status_safe(item_id, final_status)
            raise
        except Exception as exc:
            refreshed_batch = await self.batch_registry.get_batch(batch_id)
            stop_requested = bool(event.get_extra("agent_stop_requested")) or bool(
                refreshed_batch and refreshed_batch.stop_requested
            )
            if stop_requested:
                dispatch_error = "批量子任务已按请求停止。"
                final_status = "stopped"
                logger.info(
                    "[大小姐模式] 批量子任务已按请求停止: batch_id=%s item_id=%s",
                    batch_id,
                    item_id,
                )
            else:
                dispatch_error = str(exc)
                final_status = "error"
                logger.error(
                    "[大小姐模式] 批量子任务执行失败: batch_id=%s item_id=%s error=%s",
                    batch_id,
                    item_id,
                    exc,
                    exc_info=True,
                )
            await self.batch_registry.finish_item(
                batch_id,
                item_id,
                status=final_status,
                error=dispatch_error,
                agent_name=resolved_agent_name,
            )
            await self._console_update_status_safe(
                item_id,
                final_status,
                meta={
                    "batch_id": batch_id,
                    "agent_name": resolved_agent_name,
                    "error": dispatch_error,
                },
            )
            await self._console_event_safe(
                task_id=item_id,
                event_type=("stopped" if final_status == "stopped" else "error"),
                title=("批量子任务已停止" if final_status == "stopped" else "批量子任务失败"),
                message=dispatch_error,
                source="agent",
                status=final_status,
                payload={"batch_id": batch_id, "agent_name": resolved_agent_name},
            )
        finally:
            if self.session_store:
                await self.session_store.close_session(session_id, status=final_status)

    async def _run_maid_batch_background_task(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        pending: dict,
    ) -> None:
        batch_id = str(pending.get("batch_id") or uuid.uuid4().hex)
        items = pending.get("items") or []
        maid_full_reply = str(pending.get("maid_full_reply") or "")
        true_user_input = pending.get("true_user_input")
        image_urls_raw = pending.get("image_urls_raw")
        session_done_requested = bool(pending.get("session_done_requested", False))
        reasoning_content = str(pending.get("reasoning_content", "") or "")
        reasoning_signature = pending.get("reasoning_signature")
        agent_id = str(pending.get("agent_id") or batch_id)
        parent_message_id = str(pending.get("parent_message_id") or "")
        source = str(pending.get("source") or "chat")
        watchdog: asyncio.Task | None = None

        if not isinstance(items, list) or not items:
            await self.background_tasks.finish(
                batch_id,
                status="error",
                error="批量任务缺少可执行条目",
            )
            await self._console_update_status_safe(
                batch_id,
                "error",
                meta={"error": "批量任务缺少可执行条目"},
            )
            await self._console_event_safe(
                task_id=batch_id,
                event_type="error",
                title="批量任务缺少可执行条目",
                source="system",
                status="error",
            )
            return

        try:
            batch = await self.batch_registry.create_batch(
                batch_id=batch_id,
                unified_msg_origin=event.unified_msg_origin,
                sender_id=event.get_sender_id(),
                maid_full_reply=maid_full_reply,
                true_user_input=(true_user_input if isinstance(true_user_input, str) else None),
                image_urls_raw=image_urls_raw,
                session_done_requested=session_done_requested,
                reasoning_content=reasoning_content,
                reasoning_signature=(
                    str(reasoning_signature) if reasoning_signature is not None else None
                ),
                items=items,
            )
            await self.batch_registry.mark_batch_running(batch_id)
            await self.background_tasks.mark_running(
                batch_id,
                progress=f"批量管家任务开始执行，共 {len(batch.items)} 项",
            )
            await self._console_update_status_safe(batch_id, "running")
            await self._console_event_safe(
                task_id=batch_id,
                event_type="running",
                title="批量管家任务开始执行",
                message=f"共 {len(batch.items)} 项",
                source="runner",
                status="running",
            )
            watchdog = self._start_dispatch_watchdog(
                task_id=batch_id,
                source=source,
                event=event,
            )
            for item in batch.items:
                await self._console_ensure_task_safe(
                    self._build_task_patch(
                        task_id=item.item_id,
                        kind="batch_item",
                        source="chat",
                        unified_msg_origin=batch.unified_msg_origin,
                        sender_id=batch.sender_id,
                        agent_name=item.agent_name,
                        status=item.status,
                        request_text=item.maid_request,
                        parent_task_id=batch.batch_id,
                        title=f"批量子任务: {item.agent_name}",
                        meta={
                            "batch_id": batch.batch_id,
                            "session_id": item.session_id,
                            "agent_id": item.item_id,
                            "parent_agent_id": batch.batch_id,
                            "parent_message_id": parent_message_id,
                        },
                    )
                )
                await self._console_event_safe(
                    task_id=item.item_id,
                    event_type="queued",
                    title="批量子任务已排队",
                    message=item.maid_request,
                    source="chat",
                    status=item.status,
                    payload={
                        "batch_id": batch.batch_id,
                        "agent_name": item.agent_name,
                        "agent_id": item.item_id,
                        "parent_agent_id": batch.batch_id,
                        "parent_message_id": parent_message_id,
                    },
                )

            item_tasks = [
                asyncio.create_task(
                    self._run_maid_batch_item_background_task(
                        event=event,
                        batch_id=batch.batch_id,
                        item_id=item.item_id,
                        session_id=item.session_id,
                        maid_full_reply=batch.maid_full_reply,
                        agent_name=item.agent_name,
                        maid_request=item.maid_request,
                        true_user_input=batch.true_user_input,
                        image_urls_raw=batch.image_urls_raw,
                        parent_message_id=parent_message_id,
                    )
                )
                for item in batch.items
            ]
            if item_tasks:
                await asyncio.gather(*item_tasks, return_exceptions=True)

            batch = await self.batch_registry.get_batch(batch_id)
            if batch is None:
                raise RuntimeError(f"批量任务记录不存在: {batch_id}")

            batch_result = self._build_batch_follow_up_result(batch)
            maid_visible_text = batch.maid_full_reply.strip()
            await self._console_event_safe(
                task_id=batch_id,
                event_type="follow_up_request",
                title="请求大小姐整理批量结果",
                message=batch_result,
                source="system",
                status=batch.status,
            )
            follow_up_resp = await self._request_maid_follow_up(
                event=event,
                req=req,
                maid_visible_text=maid_visible_text,
                agent_name="batch",
                maid_request="批量管家结果汇总",
                agent_result=batch_result,
                reasoning_content=batch.reasoning_content,
                reasoning_signature=batch.reasoning_signature,
            )
            follow_up_completion_text = follow_up_resp.completion_text or ""
            sanitized_follow_up = follow_up_completion_text.strip()
            if sanitized_follow_up != follow_up_completion_text and sanitized_follow_up:
                self._rewrite_response_text(follow_up_resp, sanitized_follow_up)

            if follow_up_resp.result_chain is not None or sanitized_follow_up.strip():
                chain = follow_up_resp.result_chain or MessageChain(
                    chain=[Comp.Plain(sanitized_follow_up)]
                )
                await event.send(chain)
                await self._persist_assistant_reply(
                    event,
                    req,
                    sanitized_follow_up or follow_up_completion_text,
                    agent_id=agent_id,
                )
                await self._persist_call_maid_tool_history(
                    event,
                    req,
                    [
                        {
                            "action": "dispatch",
                            "request_text": "批量管家结果汇总",
                            "agent_name": "batch",
                            "tool_result": batch_result,
                        }
                    ],
                    agent_id=agent_id,
                )
                await self._console_event_safe(
                    task_id=batch_id,
                    event_type="follow_up_sent",
                    title="批量结果追答已发送",
                    message=sanitized_follow_up or follow_up_completion_text,
                    source="chat",
                    status=batch.status,
                )

            await self.background_tasks.finish(
                batch_id,
                status=batch.status,
                result=sanitized_follow_up or batch_result,
            )
            await self._console_update_status_safe(
                batch_id,
                batch.status,
                meta={
                    "batch_result": batch_result,
                    "follow_up": sanitized_follow_up or follow_up_completion_text,
                },
            )
            await self._console_event_safe(
                task_id=batch_id,
                event_type=("error" if batch.status == "error" else "finished"),
                title="批量管家任务完成",
                message=sanitized_follow_up or batch_result,
                source="system",
                status=batch.status,
            )
        except asyncio.CancelledError:
            await self.background_tasks.finish(
                batch_id,
                status="stopped",
                error="批量后台任务已取消。",
            )
            await self._console_update_status_safe(
                batch_id,
                "stopped",
                meta={"error": "批量后台任务已取消。"},
            )
            raise
        except Exception as exc:
            await self.background_tasks.finish(
                batch_id,
                status="error",
                error=str(exc),
            )
            await self._console_update_status_safe(
                batch_id,
                "error",
                meta={"error": str(exc)},
            )
            await self._console_event_safe(
                task_id=batch_id,
                event_type="error",
                title="批量后台追答任务失败",
                message=str(exc),
                source="system",
                status="error",
            )
            logger.error("[大小姐模式] 批量后台追答任务失败: %s", exc, exc_info=True)
        finally:
            await self._cancel_dispatch_watchdog(watchdog)
            await self.batch_registry.discard_batch(batch_id)
            self._batch_runners_by_batch_id.pop(batch_id, None)
            self._stop_requested_batch_ids.discard(batch_id)

    async def _build_background_status_text(self, event: AstrMessageEvent) -> str:
        current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        active = await self.background_tasks.list_active()
        lines = [f"当前后台管家任务数: {len(active)}"]
        if current is None:
            lines.append("当前会话没有运行中的管家任务。")
            return "\n".join(lines)

        lines.append(f"当前任务ID: {current.task_id}")
        lines.append(f"状态: {current.status}")
        lines.append(f"类型: {current.kind}")
        if current.kind == "batch":
            batch = await self.batch_registry.get_batch(current.task_id)
            if batch is None:
                lines.append("批量任务详情暂不可用。")
                return "\n".join(lines)
            lines.append(f"Batch ID: {batch.batch_id}")
            lines.append(f"Batch 状态: {batch.status}")
            lines.append(f"子任务总数: {len(batch.items)}")
            lines.append(
                "运行中/完成/失败/停止: "
                f"{sum(item.status == 'running' for item in batch.items)}/"
                f"{sum(item.status == 'done' for item in batch.items)}/"
                f"{sum(item.status == 'error' for item in batch.items)}/"
                f"{sum(item.status == 'stopped' for item in batch.items)}"
            )
            for item in batch.items:
                lines.append(
                    f"- item={item.item_id[:8]} agent={item.agent_name} status={item.status} session={item.session_id[:8]}"
                )
                if item.status == "running" and item.last_assistant_output:
                    lines.append(f"  最新 assistant 输出: {item.last_assistant_output[:50]}")
                elif item.result:
                    lines.append(f"  最新结果: {item.result[:120]}")
                elif item.error:
                    lines.append(f"  错误: {item.error[:120]}")
                else:
                    lines.append(f"  请求: {item.maid_request[:120]}")
            return "\n".join(lines)

        lines.append(f"Agent: {current.agent_name}")
        if current.status == "running" and current.last_assistant_output:
            lines.append(f"最新 assistant 输出: {current.last_assistant_output[:50]}")
        elif current.last_progress:
            lines.append(f"进度: {current.last_progress}")
        elif current.last_assistant_output:
            lines.append(f"最新 assistant 输出: {current.last_assistant_output[:50]}")
        elif current.last_agent_result:
            lines.append(f"最新结果: {current.last_agent_result[:120]}")
        elif current.error:
            lines.append(f"错误: {current.error[:120]}")
        else:
            lines.append(f"请求: {current.maid_request[:120]}")
        return "\n".join(lines)

    async def _request_stop_background_tasks(
        self,
        event: AstrMessageEvent,
        *,
        source: str = "chat",
    ) -> str:
        current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        if current is None:
            return "当前会话没有运行中的管家任务。"
        if source == "chat" and event.get_sender_id() != current.sender_id:
            return "当前后台管家任务不属于本次发言的对方，无法停止。"

        if current.kind == "batch":
            await self.batch_registry.request_stop(current.task_id)
            self._stop_requested_batch_ids.add(current.task_id)
            batch_runners = list(
                (self._batch_runners_by_batch_id.get(current.task_id) or {}).values()
            )
            for runner in batch_runners:
                try:
                    runner.request_stop()
                except Exception as exc:
                    logger.warning(
                        "[大小姐模式] 批量 runner 停止请求失败: batch_id=%s error=%s",
                        current.task_id,
                        exc,
                    )
            await self.background_tasks.update_progress(
                current.task_id,
                "已收到批量任务停止请求，等待当前步骤结束后中断。",
            )
            await self._console_update_status_safe(current.task_id, "stopping")
            await self._console_event_safe(
                task_id=current.task_id,
                event_type="stop_requested",
                title="请求停止批量任务",
                message="已收到批量任务停止请求，等待当前步骤结束后中断。",
                source=source,
                status="stopping",
            )
            lines = [f"已请求停止当前会话的批量管家任务。batch_id={current.task_id}"]
            lines.append(f"命中的批量执行器数: {len(batch_runners)}")
            lines.append("批量任务中的所有仍在运行的子任务都会在当前步骤结束后尝试停止。")
            return "\n".join(lines)

        stopped = 0
        runner = self._background_runners_by_umo.get(event.unified_msg_origin)
        if runner is not None:
            runner_event = self._background_runner_events_by_runner_id.get(id(runner))
            if runner_event is not None:
                runner_event.set_extra("agent_stop_requested", True)
                stopped += 1
            try:
                runner.request_stop()
            except Exception as exc:
                logger.warning(
                    "[大小姐模式] 单任务 runner 停止请求失败: task_id=%s error=%s",
                    current.task_id,
                    exc,
                )
        if stopped == 0:
            stopped += active_event_registry.request_agent_stop_all(
                event.unified_msg_origin,
                exclude=event,
            )
        await self.background_tasks.update_progress(
            current.task_id,
            "已收到停止请求，等待当前步骤结束后中断。",
        )
        await self._console_update_status_safe(current.task_id, "stopping")
        await self._console_event_safe(
            task_id=current.task_id,
            event_type="stop_requested",
            title="请求停止后台任务",
            message="已收到停止请求，等待当前步骤结束后中断。",
            source=source,
            status="stopping",
        )
        lines = [f"已请求停止当前会话的后台管家任务。task_id={current.task_id}"]
        lines.append(f"命中的活跃事件数: {stopped}")
        lines.append("如果子 agent 正在等待工具或网络返回，通常会在当前步骤结束后停止。")
        return "\n".join(lines)

    async def _steer_background_task(
        self,
        event: AstrMessageEvent,
        message_text: str,
        *,
        source: str = "chat",
    ) -> str:
        current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        if current is None:
            return "当前会话没有运行中的管家任务，无法补充要求。"
        if current.kind == "batch":
            return "当前任务为批量管家任务，暂不支持补充要求，请等待完成或使用 /maid stop 停止整批任务。"

        runner = self._background_runners_by_umo.get(event.unified_msg_origin)
        if runner is None:
            return "当前没有可引导的活跃管家执行器，请稍后再试。"

        runner_event = self._background_runner_events_by_runner_id.get(id(runner))
        active_sender_id = runner_event.get_sender_id() if runner_event is not None else None
        sender_id = event.get_sender_id()
        if source == "chat" and sender_id != current.sender_id:
            return "当前后台管家任务不属于本次发言的对方，无法补充要求。"
        if source != "chat" and sender_id != active_sender_id and current.sender_id != "dashboard":
            return "当前后台管家任务不属于本次操作来源，无法补充要求。"

        ticket = runner.follow_up(message_text=message_text)
        if ticket is None:
            return "当前后台管家任务暂时无法接收补充要求。"

        await self.background_tasks.update_progress(
            current.task_id,
            f"已收到新的补充要求: {message_text[:120]}",
        )
        await self._console_event_safe(
            task_id=current.task_id,
            event_type="steer",
            title="补充要求",
            message=message_text,
            source=source,
            status=current.status,
        )
        return (
            f"已将补充要求转交给后台管家。task_id={current.task_id}\n补充内容: {message_text[:120]}"
        )

    def _register_background_runner(self, umo: str, runner: object) -> None:
        self._background_runners_by_umo[umo] = runner
        runner_context = getattr(runner, "run_context", None)
        wrapped_context = getattr(runner_context, "context", None)
        runner_event = getattr(wrapped_context, "event", None)
        if runner_event is not None:
            self._background_runner_events_by_runner_id[id(runner)] = runner_event

    def _unregister_background_runner(self, umo: str, runner: object) -> None:
        self._background_runner_events_by_runner_id.pop(id(runner), None)
        if self._background_runners_by_umo.get(umo) is runner:
            self._background_runners_by_umo.pop(umo, None)

    def _register_batch_runner(self, batch_id: str, runner: object) -> None:
        self._batch_runners_by_batch_id.setdefault(batch_id, {})[id(runner)] = runner
        if batch_id in self._stop_requested_batch_ids:
            try:
                runner.request_stop()
            except Exception as exc:
                logger.warning(
                    "[大小姐模式] 延迟注册的批量 runner 停止请求失败: batch_id=%s error=%s",
                    batch_id,
                    exc,
                )

    def _unregister_batch_runner(self, batch_id: str, runner: object) -> None:
        runners = self._batch_runners_by_batch_id.get(batch_id)
        if runners is None:
            return
        runners.pop(id(runner), None)
        if not runners:
            self._batch_runners_by_batch_id.pop(batch_id, None)

    @filter.on_llm_response()
    async def sanitize_llm_response(
        self,
        _event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        if self.maid_mode_config.log_raw_llm_io:
            logger.debug(
                "[大小姐模式] LLM响应原文:\n%s",
                self._dump_json(
                    {
                        "completion_text": resp.completion_text,
                        "tools_call_name": resp.tools_call_name,
                        "tools_call_args": resp.tools_call_args,
                        "tools_call_ids": resp.tools_call_ids,
                        "tools_call_extra_content": resp.tools_call_extra_content,
                        "reasoning_content": resp.reasoning_content,
                    }
                ),
            )

    @filter.after_message_sent()
    async def continue_maid_follow_up_after_send(self, event: AstrMessageEvent) -> None:
        try:
            logger.debug("[大小姐模式] after_message_sent 进入后续处理")
            if event.get_platform_name() not in {"cron", "dashboard"}:
                await self.outbox.note_user_message(event.unified_msg_origin)
            req = event.get_extra("provider_request")
            if not self._is_provider_request_like(req):
                return

            tool_history_records = self._consume_call_maid_tool_history(event)
            if tool_history_records:
                await self._persist_call_maid_tool_history(event, req, tool_history_records)

            pending_items = self._consume_pending_dispatches(event)
            if not pending_items:
                return

            maid_full_reply = self._extract_latest_assistant_text(event)
            true_user_input = event.get_extra(TRUE_USER_INPUT_EXTRA_KEY, "") or ""
            image_urls_raw = getattr(getattr(event, "message_obj", None), "image_urls", None)

            current_task = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
            if current_task is not None:
                logger.warning(
                    "[大小姐模式] 当前会话已有后台任务运行，将新的 call_maid(dispatch) 降级为补充要求: current_task_id=%s",
                    current_task.task_id,
                )
                maid_request = (
                    self._join_batch_steer_text(pending_items)
                    if len(pending_items) > 1
                    else pending_items[0]["maid_request"]
                )
                steer_text = await self._steer_background_task(event, maid_request)
                await self._console_event_safe(
                    task_id=current_task.task_id,
                    event_type="steer",
                    title="新 dispatch 降级为补充要求",
                    message=maid_request,
                    source="chat",
                    status=current_task.status,
                )
                if steer_text.strip():
                    await event.send(MessageChain(chain=[Comp.Plain(steer_text)]))
                return

            if len(pending_items) > 1:
                pending = {
                    "mode": "batch",
                    "items": pending_items,
                    "maid_full_reply": maid_full_reply,
                    "true_user_input": (
                        true_user_input if self.maid_mode_config.include_raw_user_input else None
                    ),
                    "image_urls_raw": image_urls_raw,
                    "session_done_requested": False,
                    "parent_message_id": pending_items[0].get("parent_message_id", ""),
                }
                kind = "batch"
            else:
                pending = {
                    "agent_name": pending_items[0]["agent_name"],
                    "maid_full_reply": maid_full_reply,
                    "maid_request": pending_items[0]["maid_request"],
                    "true_user_input": (
                        true_user_input if self.maid_mode_config.include_raw_user_input else None
                    ),
                    "image_urls_raw": image_urls_raw,
                    "session_done_requested": False,
                    "parent_message_id": pending_items[0].get("parent_message_id", ""),
                }
                kind = "single"

            try:
                task_id = await self._launch_maid_dispatch(
                    source="chat",
                    event=event,
                    req=req,
                    pending=pending,
                    kind=kind,
                )
            except MaidTaskConflictError as conflict:
                maid_request = (
                    self._join_batch_steer_text(pending_items)
                    if len(pending_items) > 1
                    else pending_items[0]["maid_request"]
                )
                steer_text = await self._steer_background_task(event, maid_request)
                await self._console_event_safe(
                    task_id=conflict.active_task.task_id,
                    event_type="steer",
                    title="并发 dispatch 降级为补充要求",
                    message=maid_request,
                    source="chat",
                    status=conflict.active_task.status,
                )
                if steer_text.strip():
                    await event.send(MessageChain(chain=[Comp.Plain(steer_text)]))
                return

            logger.debug(
                "[大小姐模式] 已投递后台管家任务，kind=%s task_id=%s，主链路不再等待执行完成",
                kind,
                task_id,
            )

        except Exception as exc:
            logger.error("[大小姐模式] after_message_sent 后续追答失败: %s", exc, exc_info=True)
        finally:
            self._clear_pending_follow_up(event)

    @filter.command_group("maid")
    def maid(self):
        pass

    @maid.command("status")
    async def maid_status(self, event: AstrMessageEvent):
        active = await self.orchestrator.list_active_runs(
            event.unified_msg_origin,
            event.get_sender_id(),
        )
        if active:
            lines = ["当前活跃 MaidAgent runs："]
            lines.extend(
                f"- agent={run.agent_id[:8]} task={run.task_id[:8]} "
                f"status={run.status} mode={run.mode}"
                for run in active
            )
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result(await self._build_background_status_text(event))

    @maid.command("stop")
    async def maid_stop(self, event: AstrMessageEvent):
        active = await self.orchestrator.list_active_runs(
            event.unified_msg_origin,
            event.get_sender_id(),
        )
        if active:
            for run in active:
                await self.orchestrator.stop(
                    task_id=run.task_id,
                    event=event,
                )
            yield event.plain_result(f"已请求停止 {len(active)} 个活跃 run。")
            return
        yield event.plain_result(await self._request_stop_background_tasks(event))

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """
        结果装饰阶段。

        当前仅用于日志记录。
        """
        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY)
        if raw_input:
            logger.debug("[大小姐模式] 本轮对话原始输入: %s...", raw_input[:100])

        result = event.get_result()
        if result and result.async_stream:
            original_stream = result.async_stream

            async def wrapped_stream():
                try:
                    async for chunk in original_stream:
                        yield chunk
                finally:
                    logger.debug("[大小姐模式] 流式输出已结束，开始触发后台后续处理")
                    task = asyncio.create_task(self.continue_maid_follow_up_after_send(event))
                    self._track_background_task(task)

            result.async_stream = wrapped_stream()
