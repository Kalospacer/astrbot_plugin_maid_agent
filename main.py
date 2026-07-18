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

from quart import Response as QuartResponse
from quart import jsonify, make_response, request

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astr_message_event import AstrMessageEvent as CoreAstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.history_saver import persist_agent_history

from .config import _safe_int, load_maid_mode_config, render_dispatch_prompt
from .constants import (
    CALL_MAID_TOOL_NAME,
    MAID_NOTIFICATION_ID_META_KEY,
    MAID_NOTIFICATION_IDS_META_KEY,
    MAID_TASK_TOOL_NAME,
    MISTRESS_REQUEST_BLOCK_LABEL,
    PLUGIN_DATA_DIR_NAME,
    RAW_INPUT_EXTRA_KEY,
    TRUE_USER_INPUT_EXTRA_KEY,
    USER_INPUT_BLOCK_LABEL,
)
from .json_io import dump_json
from .maid_dispatcher import (
    _get_compress_provider,
    _normalize_begin_dialogs,
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
from .runtime_store import (
    CTRL_RUN_END,
    CTRL_RUN_START,
    CTRL_STEER,
    RunMeta,
    RuntimeStore,
)
from .sse_hub import SseHub
from .toolset_adapter import (
    _agent_memory_enabled as _agent_memory_enabled_fn,
)
from .toolset_adapter import (
    _load_provider_settings as _load_provider_settings_fn,
)
from .toolset_adapter import (
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

    def __init__(
        self,
        original: AstrMessageEvent,
        *,
        send_target: CoreAstrMessageEvent | None = None,
    ) -> None:
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
        self._send_target = send_target

    async def send(self, message: MessageChain) -> None:
        try:
            text = message.get_plain_text()
        except Exception:
            text = str(message)
        self.sent_messages.append(text)
        if self._send_target is not None:
            await self._send_target.send(message)


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
        self.sse_hub = SseHub()
        self._active_asyncio_tasks: set[asyncio.Task] = set()
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

    async def initialize(self) -> None:
        """插件初始化"""
        # 1.3.0 runtime: collapse orphaned runs and retry pending notifications.
        reconciled_runs = await self.runtime_store.reconcile_on_restart()
        if reconciled_runs:
            logger.warning(
                "[大小姐模式] 启动时收敛 %d 个遗留 runtime run 为 interrupted: %s",
                len(reconciled_runs),
                ",".join(run.task_id[:8] for run in reconciled_runs),
            )
        await self.outbox.on_restart()
        self._patch_runtime_tool_schemas()
        self._schedule_retention_cleanup()
        self._register_console_web_apis()
        logger.info(
            "[MaidAgent] 已加载 (1.4.0) | default_agent=%s | allowed_agents=%s | hide_native_tools=%s | hide_transfer_tools=%s | include_raw_user_input=%s | log_raw_llm_io=%s | fg_timeout=%ss | memory_agents=%s | capacity=%s/%s | retention=%dd",
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

        tasks = [task for task in self._active_asyncio_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._active_asyncio_tasks.clear()
        await self.sse_hub.close()

    async def _on_runtime_terminal(self, run: RunMeta) -> None:
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
        provider_id = getattr(
            handoff, "provider_id", None
        ) or await context.get_current_chat_provider_id(umo)
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
        tool_call_timeout = _safe_int(provider_settings.get("tool_call_timeout", 60), 60)
        agent_max_step = _safe_int(provider_settings.get("max_agent_step", 30), 30)
        image_urls = await collect_child_image_urls(event, image_urls_raw)
        user_input_block = (
            f"【{USER_INPUT_BLOCK_LABEL}】\n{true_user_input}\n\n" if true_user_input.strip() else ""
        )
        maid_request_block = f"【{MISTRESS_REQUEST_BLOCK_LABEL}】\n{request_text}\n\n"
        dispatch_prompt = render_dispatch_prompt(
            self.maid_mode_config.dispatch_prompt_template,
            user_input_block=user_input_block,
            maid_request_block=maid_request_block,
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
                llm_compress_instruction=str(
                    provider_settings.get("llm_compress_instruction", "") or ""
                ),
                llm_compress_keep_recent=_safe_int(
                    provider_settings.get("llm_compress_keep_recent", 4), 4
                ),
                llm_compress_provider=_get_compress_provider(context, provider_settings),
                truncate_turns=_safe_int(provider_settings.get("dequeue_context_length", 1), 1),
                enforce_max_turns=_safe_int(provider_settings.get("max_context_length", -1), -1),
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
                {
                    "role": "user",
                    "content": dispatch_prompt,
                    "task_id": run.task_id,
                    # 结构化真源：控制台据此拆出「大小姐」独立气泡，
                    # 无需事后从拼接串里解析（模板正文紧跟大小姐请求块，解析不可靠）。
                    "user_input": true_user_input,
                    "mistress_request": request_text,
                },
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
            await self.sse_hub.publish(
                {
                    "type": "runtime_trace",
                    "agent_id": run.agent_id,
                    "task_id": run.task_id,
                    "status": current.status if current is not None else run.status,
                    "tool_chain": tool_chain,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[大小姐模式] 发布 runtime 工具调用链失败: task_id=%s err=%s",
                run.task_id,
                exc,
            )

    def _isolate_child_event(
        self,
        event: AstrMessageEvent,
        *,
        forward_sends: bool = False,
    ):
        """Return a child event sharing UMO/sender/role/group/platform but with
        isolated extras/result/stop/tempfile state."""
        return _IsolatedMaidEvent(
            event,
            send_target=event if forward_sends else None,
        )

    def _resolve_handoff_for_runtime(self, agent_name: str):
        from .maid_dispatcher import _resolve_handoff

        return _resolve_handoff(
            self.context,
            agent_name,
            fallback_agent_name=self.maid_mode_config.default_agent_name,
        )

    def _load_provider_settings(self, umo: str) -> dict:
        return _load_provider_settings_fn(self.context, umo)

    def _ensure_provider_max_context_tokens(self, provider) -> int:
        from .maid_dispatcher import _ensure_provider_max_context_tokens

        return _ensure_provider_max_context_tokens(provider)

    def _agent_memory_enabled(self, agent_name: str) -> bool:
        return _agent_memory_enabled_fn(self.maid_mode_config.memory_agent_names, agent_name)

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
                tool_call_timeout=_safe_int(
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
                f"{prefix}/agents/<agent_id>/transcript",
                self.console_agent_transcript,
                ["GET"],
                "Get the full conversation transcript of a 1.3.0 runtime agent",
            ),
            (
                f"{prefix}/agents/<agent_id>/export",
                self.console_agent_export,
                ["GET"],
                "Download the conversation transcript of a 1.3.0 runtime agent",
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
        return asdict(config)

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
            record.get("_control") and record.get("kind") == "tool_start" for record in records
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

    async def console_overview(self):
        agent_ids = await self.runtime_store.list_agent_ids()
        agents = [
            meta
            for agent_id in agent_ids
            if (meta := await self.runtime_store.load_agent(agent_id)) is not None
        ]
        active = [meta for meta in agents if meta.active_task_id]
        overview = {
            "agent_count": len(agents),
            "active_agent_count": len(active),
            "umo_count": len({meta.unified_msg_origin for meta in agents if meta.unified_msg_origin}),
            "config": self._config_payload(self.maid_mode_config),
        }
        return self._console_ok(overview)

    async def console_stream(self):
        queue = await self.sse_hub.subscribe()

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
                await self.sse_hub.unsubscribe(queue)

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
        try:
            meta = await self.runtime_store.load_agent(agent_id)
            if meta is None:
                return self._console_error("agent 不存在。", status_code=404)
            runs = [run.to_dict() for run in await self.runtime_store.list_runs(agent_id)]
        except ValueError as exc:
            return self._console_error(str(exc), status_code=400)
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

    @staticmethod
    def _split_dispatch_blocks(content: str) -> tuple[str, str]:
        """从旧版拼接 dispatch_prompt 中兜底解析出 ``(对方原话, 大小姐请求)``。

        仅用于缺少结构化字段的历史 transcript；新数据直接走消息上的
        ``user_input`` / ``mistress_request`` 字段。无任何标记时整串视作对方原话。

        :param content: 女仆首条 user 消息的文本内容。
        :returns: ``(user_text, mistress_text)``，缺失的一段为空串。
        """
        text = str(content or "")
        user_marker = f"【{USER_INPUT_BLOCK_LABEL}】"
        mistress_marker = f"【{MISTRESS_REQUEST_BLOCK_LABEL}】"
        user_idx = text.find(user_marker)
        mistress_idx = text.find(mistress_marker)
        if user_idx == -1 and mistress_idx == -1:
            return text.strip(), ""

        def _block(start_idx: int, marker: str) -> str:
            if start_idx == -1:
                return ""
            rest = text[start_idx + len(marker) :]
            next_idx = rest.find("【")
            return (rest if next_idx == -1 else rest[:next_idx]).strip()

        return _block(user_idx, user_marker), _block(mistress_idx, mistress_marker)

    def _extract_dispatch_texts(self, record: dict[str, Any]) -> tuple[str, str]:
        """取女仆首条 user 消息的 ``(对方原话, 大小姐请求)`` 两段纯文本。

        优先使用落库时写入的结构化字段（``user_input`` / ``mistress_request``）；
        历史记录缺字段时回退到解析拼接 content。

        :param record: 一条 ``role == "user"`` 的 transcript 记录。
        :returns: ``(user_text, mistress_text)``。
        """
        if "mistress_request" in record or "user_input" in record:
            user_text = str(record.get("user_input") or "").strip()
            mistress_text = str(record.get("mistress_request") or "").strip()
            return user_text, mistress_text
        return self._split_dispatch_blocks(
            self._message_content_to_text(record.get("content"))
        )

    def _build_agent_transcript_payload(
        self,
        agent_id: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Split the agent's full transcript into per-run conversation segments.

        Records are attributed by, in order: the record's own ``task_id``, the
        currently open ``run_start`` segment, or the most recent segment.
        ``steer`` control records become user-side bubbles of their segment.
        """
        segments: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        current_id = ""

        def _new_segment(task_id: str) -> dict[str, Any]:
            segment = {
                "task_id": task_id,
                "user_text": "",
                "mistress_text": "",
                "steers": [],
                "records": [],
                "orphan_count": 0,
                "_dispatch_captured": False,
            }
            segments[task_id] = segment
            order.append(task_id)
            return segment

        def _fallback_segment() -> dict[str, Any] | None:
            if current_id:
                return segments[current_id]
            if order:
                return segments[order[-1]]
            return None

        for record in records:
            if record.get("_control"):
                kind = str(record.get("kind") or "")
                record_task_id = str(record.get("task_id") or "").strip().casefold()
                if kind == CTRL_RUN_START and record_task_id:
                    _new_segment(record_task_id)
                    current_id = record_task_id
                    continue
                if kind == CTRL_RUN_END and record_task_id:
                    if record_task_id == current_id:
                        current_id = ""
                    continue
                if kind == CTRL_STEER:
                    text = str(record.get("message") or "").strip()
                    target = _fallback_segment()
                    if text and target is not None:
                        target["steers"].append(text)
                    continue
                # tool_start / tool_end 等其他控制记录按归属进段，
                # 交由 _build_runtime_tool_chain_payload 消费。
                target = _fallback_segment()
                if target is not None:
                    target["records"].append(record)
                continue

            record_task_id = str(record.get("task_id") or "").strip().casefold()
            target = segments.get(record_task_id) if record_task_id else None
            if target is None:
                target = _fallback_segment()
                if target is not None:
                    target["orphan_count"] += 1
            if target is None:
                # 首个 run_start 之前的记录（如 begin_dialogs）：暂存到虚拟段，
                # 之后并入第一个真实 segment，保证不丢消息。
                target = segments.get("")
                if target is None:
                    target = _new_segment("")
                target["records"].append(record)
                continue
            target["records"].append(record)
            if not target.get("_dispatch_captured") and str(record.get("role") or "") == "user":
                # 仅首条 user 记录承载派活内容；user_text 现在可能合法为空
                # （没有对方原话），故用显式标志位而非 user_text 判空。
                target["_dispatch_captured"] = True
                target["user_text"], target["mistress_text"] = self._extract_dispatch_texts(record)

        # 把 run_start 之前的虚拟段并入第一个真实 segment（没有真实段则保留）。
        leading = segments.pop("", None)
        if leading is not None:
            order = [task_id for task_id in order if task_id]
            if order:
                first = segments[order[0]]
                first["records"] = leading["records"] + first["records"]
                first["orphan_count"] += leading["orphan_count"]
                if not first.get("_dispatch_captured") and leading.get("_dispatch_captured"):
                    first["user_text"] = leading["user_text"]
                    first["mistress_text"] = leading["mistress_text"]
                    first["_dispatch_captured"] = True
            else:
                order.append("")
                segments[""] = leading

        runs = []
        for task_id in order:
            segment = segments[task_id]
            runs.append(
                {
                    "task_id": segment["task_id"],
                    "user_text": segment["user_text"],
                    "mistress_text": segment["mistress_text"],
                    "steers": segment["steers"],
                    "tool_chain": self._build_runtime_tool_chain_payload(segment["records"]),
                    "orphan_count": segment["orphan_count"],
                }
            )
        return {"agent_id": agent_id, "runs": runs}

    def _resolve_mistress_name(self) -> str:
        """控制台「大小姐」气泡的显示名 = 全局默认人格名。

        取 ``provider_settings.default_personality``；未命名（``default`` 占位）
        或 context 不可用时返回空串，由前端决定是否省略名称标签。

        :returns: 默认人格名，或空串。
        """
        try:
            manager = getattr(getattr(self, "context", None), "persona_manager", None)
            name = str(getattr(manager, "default_persona", "") or "").strip()
        except Exception:
            return ""
        return "" if name == "default" else name

    async def console_agent_transcript(self, agent_id: str):
        try:
            meta = await self.runtime_store.load_agent(agent_id)
            if meta is None:
                return self._console_error("agent 不存在。", status_code=404)
            records = await self.runtime_store.load_transcript(agent_id)
            payload = self._build_agent_transcript_payload(meta.agent_id, records)
            payload["mistress_name"] = self._resolve_mistress_name()
        except ValueError as exc:
            return self._console_error(str(exc), status_code=400)
        return self._console_ok(payload)

    async def console_agent_export(self, agent_id: str):
        try:
            meta = await self.runtime_store.load_agent(agent_id)
            if meta is None:
                return self._console_error("agent 不存在。", status_code=404)
            records = await self.runtime_store.load_transcript(agent_id)
            payload = self._build_agent_transcript_payload(meta.agent_id, records)
            payload["mistress_name"] = self._resolve_mistress_name()
            payload["agent"] = meta.to_dict()
            payload["runs_meta"] = [
                run.to_dict() for run in await self.runtime_store.list_runs(meta.agent_id)
            ]
        except ValueError as exc:
            return self._console_error(str(exc), status_code=400)
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        filename = f"maid-agent-{meta.agent_id[:8]}-transcript.json"
        response = await make_response(
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
        return response

    async def console_delete_agent(self, agent_id: str):
        try:
            body = await self._console_json_body()
            normalized = str(agent_id or "").strip().casefold()
            confirmation = str(body.get("confirm_agent_id") or "").strip().casefold()
            if not normalized or confirmation != normalized:
                return self._console_error("删除确认无效，请重新确认 Agent ID。")
            removed_runs = await self.orchestrator.delete_agent(normalized)
            return self._console_ok(
                {
                    "agent_id": normalized,
                    "removed_runs": removed_runs,
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
                return self._console_error(
                    f"未找到 task_id={task_id} 对应的 agent。", status_code=404
                )
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

    @staticmethod
    def _contains_agent_name(agent_names: list[str] | None, agent_name: str) -> bool:
        if not agent_names:
            return False
        target = agent_name.strip().casefold()
        return any(name.strip().casefold() == target for name in agent_names)

    @staticmethod
    def _dump_json(data) -> str:
        return dump_json(data, indent=2)

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
        tasks: list | None = None,
    ) -> str:
        """调度管家 subagent 执行任务，Claude Code 风格 foreground-first runtime。

        Args:
            request_text(string): 交给管家的任务要求。dispatch 必填；steer 时为补充要求。
            agent_name(string): 目标管家名称；留空使用默认管家。仅在新建 agent 时生效。
            resume_agent_id(string): 恢复已有 agent。填入则恢复该 agent；running 时作为 steer，终态时新建 task 后台执行。
            run_in_background(boolean): 默认 false 前台同步等待最多 foreground_timeout_seconds 秒；true 立即转后台。
            tasks(array): 批量任务列表，每项 {request_text, agent_name?, run_in_background?}，最多 5 项。仅新建 agent，不允许 resume。
        """
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
                    {
                        "status": "error",
                        "error": "batch 仅允许创建新 agent，不能使用 resume_agent_id。",
                    }
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
            return self._json_outcome({"status": "error", "error": "tasks 必须是非空列表。"})
        if len(tasks) > 5:
            return self._json_outcome(
                {"status": "error", "error": f"batch 最多 5 项，收到 {len(tasks)}。"}
            )
        requests: list[DispatchRequest] = []
        for item in tasks:
            if not isinstance(item, dict):
                return self._json_outcome({"status": "error", "error": "batch 每项必须是对象。"})
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
        for outcome in batch_outcome.items:
            if outcome.mode == "foreground" and outcome.status in {
                STATUS_COMPLETED,
                STATUS_FAILED,
                STATUS_STOPPED,
            }:
                await self.outbox.note_result_claimed(outcome.agent_id, outcome.task_id)
        return self._json_outcome(batch_outcome.to_dict())

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
            return self._json_outcome({"agent_id": agent_id, "status": "steered", "ticket": ticket})
        if normalized == "stop":
            if not task_id:
                return self._json_outcome({"status": "error", "error": "stop 需要 task_id。"})
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
        return self._json_outcome({"status": "error", "error": f"maid_task action 非法: {action}"})

    async def _resolve_agent_id_from_task(self, task_id: str) -> str:
        """Reverse-lookup an agent_id from a task_id by scanning runtime store."""
        if not task_id:
            return ""
        found = await self.runtime_store.find_run(task_id)
        return found[0].agent_id if found is not None else ""

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
        """消息发送后触发 notification outbox 的机会主义投递。"""
        try:
            if event.get_platform_name() not in {"cron", "dashboard"}:
                await self.outbox.note_user_message(event.unified_msg_origin)
        except Exception as exc:
            logger.error("[大小姐模式] after_message_sent 后续处理失败: %s", exc, exc_info=True)

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
        yield event.plain_result("当前会话没有运行中的管家任务。")

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
        yield event.plain_result("当前会话没有运行中的管家任务。")

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """结果装饰阶段。

        流式响应场景下 after_message_sent 钩子先于流结束触发，这里包装
        async_stream，在流真正结束后补一次 outbox 投递触发。
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
                    logger.debug("[大小姐模式] 流式输出已结束，触发 outbox 投递")
                    task = asyncio.create_task(self.continue_maid_follow_up_after_send(event))
                    self._track_background_task(task)

            result.async_stream = wrapped_stream()
