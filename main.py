"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 主模型不直接暴露任何原生工具
- 需要幕后执行时通过原生 `call_maid` function call 调度管家
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from quart import Response as QuartResponse
from quart import jsonify, make_response, request

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star
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

from .background_registry import MaidBackgroundTaskRegistry
from .batch_registry import MaidBatchRegistry
from .config import load_maid_mode_config
from .console_store import ConsoleTaskPatch, MaidConsoleEventStore
from .constants import (
    CALL_MAID_TOOL_NAME,
    PENDING_MAID_DISPATCHES_EXTRA_KEY,
    PENDING_MAID_FOLLOW_UP_EXTRA_KEY,
    PENDING_MAID_TOOL_HISTORY_EXTRA_KEY,
    PLUGIN_DATA_DIR_NAME,
    RAW_INPUT_EXTRA_KEY,
    TRUE_USER_INPUT_EXTRA_KEY,
)
from .maid_dispatcher import dispatch_to_maid_agent
from .session_store import MaidSessionStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context

_EMPTY_REASONING_PLACEHOLDER = " "


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
        self._conversation_history_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """插件初始化"""
        self.session_store = MaidSessionStore(self, self.maid_mode_config)
        await self.console_store.initialize()
        self._register_console_web_apis()
        logger.info(
            "[MaidAgent] 已加载 | default_agent=%s | allowed_agents=%s | hide_native_tools=%s | hide_transfer_tools=%s | include_raw_user_input=%s | session_enabled=%s | log_raw_llm_io=%s | session_timeout_minutes=%s",
            self.maid_mode_config.default_agent_name,
            ",".join(self.maid_mode_config.allowed_agent_names or []),
            self.maid_mode_config.hide_native_tools,
            self.maid_mode_config.hide_transfer_tools,
            self.maid_mode_config.include_raw_user_input,
            self.maid_mode_config.session_enabled,
            self.maid_mode_config.log_raw_llm_io,
            self.maid_mode_config.session_timeout_minutes,
        )

    async def terminate(self) -> None:
        """插件停用/重载时停止后台 runner 并取消未完成任务。"""
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
                f"{prefix}/tasks/<task_id>/events",
                self.console_task_events,
                ["GET"],
                "Maid console task events",
            ),
            (f"{prefix}/stream", self.console_stream, ["GET"], "Maid console SSE stream"),
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
            (f"{prefix}/export", self.console_export, ["GET"], "Export maid console history"),
            (f"{prefix}/clear", self.console_clear, ["POST"], "Clear maid console history"),
            (f"{prefix}/settings", self.console_settings_get, ["GET"], "Get maid settings"),
            (f"{prefix}/settings", self.console_settings_save, ["POST"], "Save maid settings"),
            (f"{prefix}/subagents", self.console_subagents, ["GET"], "List subagents"),
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
        try:
            limit = int(request.args.get("limit", 80))
        except (TypeError, ValueError):
            limit = 80
        status = str(request.args.get("status", "") or "").strip()
        query = str(request.args.get("query", "") or "").strip()
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

    async def _start_dashboard_dispatch_task(
        self,
        *,
        unified_msg_origin: str,
        agent_name: str,
        request_text: str,
        maid_full_reply: str = "",
        true_user_input: str = "",
        rerun_of: str = "",
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
        task_info = await self.background_tasks.create_task(
            unified_msg_origin=unified_msg_origin,
            sender_id="dashboard",
            agent_name=resolved_agent_name,
            maid_request=request_text,
            kind="single",
        )
        task = await self.console_store.ensure_task(
            self._build_task_patch(
                task_id=task_info.task_id,
                kind=task_info.kind,
                source="dashboard",
                unified_msg_origin=task_info.unified_msg_origin,
                sender_id=task_info.sender_id,
                agent_name=task_info.agent_name,
                status=task_info.status,
                request_text=task_info.maid_request,
                title=f"手动派发: {task_info.maid_request[:60]}",
                meta={"rerun_of": rerun_of} if rerun_of else {},
            )
        )
        await self.console_store.record_action(
            task_id=task_info.task_id,
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
        await self.console_store.record_event(
            task_id=task_info.task_id,
            event_type="queued",
            title="Dashboard 手动派发",
            message=request_text,
            source="dashboard",
            status="queued",
            payload={"agent_name": resolved_agent_name, "rerun_of": rerun_of},
        )
        runner_task = asyncio.create_task(
            self._run_dashboard_dispatch_background_task(
                {
                    "task_id": task_info.task_id,
                    "unified_msg_origin": unified_msg_origin,
                    "agent_name": resolved_agent_name,
                    "maid_request": request_text,
                    "maid_full_reply": maid_full_reply or "Dashboard 手动派发任务。",
                    "true_user_input": true_user_input or request_text,
                }
            )
        )
        self._track_background_task(runner_task)
        return task

    async def console_dispatch(self):
        try:
            body = await self._console_json_body()
            task = await self._start_dashboard_dispatch_task(
                unified_msg_origin=str(body.get("unified_msg_origin") or ""),
                agent_name=str(body.get("agent_name") or ""),
                request_text=str(body.get("request_text") or ""),
                maid_full_reply=str(body.get("maid_full_reply") or ""),
                true_user_input=str(body.get("true_user_input") or ""),
            )
            return self._console_ok({"task": task}, "已派发。")
        except Exception as exc:
            return self._console_error(str(exc))

    async def console_steer(self):
        body = await self._console_json_body()
        task_id = str(body.get("task_id") or "").strip()
        message_text = str(body.get("message_text") or "").strip()
        if not task_id or not message_text:
            return self._console_error("需要 task_id 和 message_text。")

        task = await self.console_store.get_task(task_id)
        if task is None:
            return self._console_error("任务不存在。", status_code=404)
        current = await self.background_tasks.get_active_by_umo(task["unified_msg_origin"])
        if current is None or current.task_id != task_id:
            result_text = "目标任务不是当前活跃任务，无法补充要求。"
            await self._console_action_safe(
                task_id=task_id,
                action="steer",
                payload={"message_text": message_text},
                result_text=result_text,
                status="error",
            )
            return self._console_error(result_text)
        if current.kind == "batch":
            result_text = "批量任务暂不支持补充要求。"
            await self._console_action_safe(
                task_id=task_id,
                action="steer",
                payload={"message_text": message_text},
                result_text=result_text,
                status="error",
            )
            return self._console_error(result_text)

        runner = self._background_runners_by_umo.get(task["unified_msg_origin"])
        if runner is None:
            result_text = "当前没有可引导的活跃管家执行器。"
            await self._console_action_safe(
                task_id=task_id,
                action="steer",
                payload={"message_text": message_text},
                result_text=result_text,
                status="error",
            )
            return self._console_error(result_text)
        ticket = runner.follow_up(message_text=message_text)
        if ticket is None:
            result_text = "当前后台管家任务暂时无法接收补充要求。"
            await self._console_action_safe(
                task_id=task_id,
                action="steer",
                payload={"message_text": message_text},
                result_text=result_text,
                status="error",
            )
            return self._console_error(result_text)

        await self.background_tasks.update_progress(
            task_id,
            f"Dashboard 补充要求: {message_text[:120]}",
        )
        result_text = "已将补充要求转交给后台管家。"
        await self._console_action_safe(
            task_id=task_id,
            action="steer",
            payload={"message_text": message_text},
            result_text=result_text,
        )
        await self._console_event_safe(
            task_id=task_id,
            event_type="steer",
            title="Dashboard 补充要求",
            message=message_text,
            source="dashboard",
            status=current.status,
        )
        return self._console_ok({"task_id": task_id}, result_text)

    async def console_stop(self):
        body = await self._console_json_body()
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return self._console_error("需要 task_id。")
        task = await self.console_store.get_task(task_id)
        if task is None:
            return self._console_error("任务不存在。", status_code=404)

        current = await self.background_tasks.get_active_by_umo(task["unified_msg_origin"])
        if current is None or current.task_id != task_id:
            result_text = "目标任务不是当前活跃任务。"
            await self._console_action_safe(
                task_id=task_id,
                action="stop",
                result_text=result_text,
                status="error",
            )
            return self._console_error(result_text)

        event = self._create_dashboard_event(
            unified_msg_origin=task["unified_msg_origin"],
            message_text="dashboard stop",
        )
        result_text = await self._request_stop_background_tasks(event, source="dashboard")
        await self._console_action_safe(task_id=task_id, action="stop", result_text=result_text)
        return self._console_ok({"task_id": task_id, "result": result_text}, "已请求停止。")

    async def console_done(self):
        body = await self._console_json_body()
        task_id = str(body.get("task_id") or "").strip()
        unified_msg_origin = str(body.get("unified_msg_origin") or "").strip()
        if task_id:
            task = await self.console_store.get_task(task_id)
            if task is None:
                return self._console_error("任务不存在。", status_code=404)
            unified_msg_origin = str(task.get("unified_msg_origin") or "")
        try:
            unified_msg_origin = self._validate_dashboard_umo(unified_msg_origin)
        except Exception as exc:
            return self._console_error(str(exc))

        if self.session_store:
            await self.session_store.close_active_session(unified_msg_origin, status="done")
        result_text = "当前管家 session 已结束。"
        target_task_id = task_id or "__session__"
        await self._console_action_safe(
            task_id=target_task_id,
            action="done",
            payload={"unified_msg_origin": unified_msg_origin},
            result_text=result_text,
        )
        await self._console_event_safe(
            task_id=target_task_id,
            event_type="session_done",
            title="Dashboard 结束 session",
            message=result_text,
            source="dashboard",
            status="done",
        )
        return self._console_ok({"unified_msg_origin": unified_msg_origin}, result_text)

    async def console_rerun(self):
        try:
            body = await self._console_json_body()
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                return self._console_error("需要 task_id。")
            task = await self.console_store.get_task(task_id)
            if task is None:
                return self._console_error("任务不存在。", status_code=404)
            new_task = await self._start_dashboard_dispatch_task(
                unified_msg_origin=str(task.get("unified_msg_origin") or ""),
                agent_name=str(task.get("agent_name") or ""),
                request_text=str(task.get("request_text") or ""),
                maid_full_reply="Dashboard 重新执行历史任务。",
                true_user_input=str(task.get("request_text") or ""),
                rerun_of=task_id,
            )
            return self._console_ok({"task": new_task}, "已重新派发。")
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
            "session_enabled",
            "log_raw_llm_io",
            "session_timeout_minutes",
            "dispatch_prompt_template",
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

    async def _run_dashboard_dispatch_background_task(self, pending: dict[str, Any]) -> None:
        task_id = str(pending.get("task_id") or "")
        unified_msg_origin = str(pending.get("unified_msg_origin") or "")
        agent_name = str(pending.get("agent_name") or self.maid_mode_config.default_agent_name)
        maid_request = str(pending.get("maid_request") or "")
        maid_full_reply = str(pending.get("maid_full_reply") or "")
        true_user_input = str(pending.get("true_user_input") or "")
        event = self._create_dashboard_event(
            unified_msg_origin=unified_msg_origin,
            message_text=maid_request,
        )
        final_status = "done"
        dispatch_error = ""
        agent_result = ""
        resolved_agent_name = agent_name

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
        except Exception as exc:
            dispatch_error = str(exc)
            final_status = "error"
            logger.error("[大小姐模式] Dashboard 管家任务失败: %s", exc, exc_info=True)
        finally:
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
                event_type=("error" if final_status == "error" else "finished"),
                title=("Dashboard 任务失败" if final_status == "error" else "Dashboard 任务完成"),
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
    def _get_missing_provider_request_attrs(req: object) -> list[str]:
        required = (
            "prompt",
            "image_urls",
            "contexts",
            "system_prompt",
            "model",
            "extra_user_content_parts",
        )
        return [attr for attr in required if not hasattr(req, attr)]

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
        tool_set = ToolSet()
        if call_maid_tool is not None and getattr(call_maid_tool, "active", True):
            tool_set.add_tool(call_maid_tool)

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
    ) -> int:
        pending = event.get_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY)
        items: list[dict[str, str]] = list(pending) if isinstance(pending, list) else []
        items.append(
            {
                "agent_name": agent_name,
                "maid_request": maid_request,
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

    async def _persist_assistant_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        reply_text: str,
    ) -> None:
        if not req or not getattr(req, "conversation", None) or not reply_text.strip():
            return

        try:
            history = json.loads(req.conversation.history or "[]")
        except Exception as exc:
            logger.warning("[大小姐模式] 读取主对话历史失败，无法写入后台回灌消息: %s", exc)
            return

        lock = self._conversation_history_locks.setdefault(
            event.unified_msg_origin,
            asyncio.Lock(),
        )
        async with lock:
            try:
                curr_conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin,
                    req.conversation.cid,
                )
                latest_history = json.loads(curr_conv.history or "[]") if curr_conv else history
            except Exception as exc:
                logger.warning("[大小姐模式] 读取最新主对话历史失败，使用当前快照回写: %s", exc)
                latest_history = history

            if not isinstance(latest_history, list):
                latest_history = []
            latest_history.append({"role": "assistant", "content": reply_text})
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
    ) -> None:
        if not req or not getattr(req, "conversation", None) or not records:
            return

        try:
            history = json.loads(req.conversation.history or "[]")
        except Exception as exc:
            logger.warning("[大小姐模式] 读取主对话历史失败，无法写入 call_maid 工具记录: %s", exc)
            return

        lock = self._conversation_history_locks.setdefault(
            event.unified_msg_origin,
            asyncio.Lock(),
        )
        async with lock:
            try:
                curr_conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin,
                    req.conversation.cid,
                )
                latest_history = json.loads(curr_conv.history or "[]") if curr_conv else history
            except Exception as exc:
                logger.warning(
                    "[大小姐模式] 读取最新主对话历史失败，使用当前快照回写 call_maid 工具记录: %s",
                    exc,
                )
                latest_history = history

            if not isinstance(latest_history, list):
                latest_history = []

            insertion_index = len(latest_history)
            latest_assistant_text = self._extract_latest_assistant_text(event)
            if latest_history and latest_assistant_text:
                last_message = latest_history[-1]
                if (
                    isinstance(last_message, dict)
                    and last_message.get("role") == "assistant"
                    and self._history_message_plain_text(last_message).strip()
                    == latest_assistant_text
                ):
                    insertion_index -= 1

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
        action: str,
        request_text: str = "",
        agent_name: str = "",
    ) -> str:
        """将任务交给后台管家，或控制当前后台管家任务。

        Args:
            action(string): 必填。可选 dispatch、steer、stop、done。
                dispatch 用于发起新的后台任务；
                steer 用于补充当前单个后台任务；
                stop 用于停止当前后台任务；
                done 用于结束当前管家 session。
            request_text(string): dispatch 或 steer 时填写的任务要求。stop 和 done 时留空。
            agent_name(string): dispatch 时可选。目标管家名称；留空时使用默认管家。
        """
        normalized_action = (action or "").strip().casefold()
        result_text: str
        if normalized_action not in {"dispatch", "steer", "stop", "done"}:
            result_text = "call_maid 的 action 非法，仅支持 dispatch、steer、stop、done。"
            self._queue_call_maid_tool_history(
                event,
                action=normalized_action or str(action or ""),
                request_text=request_text,
                agent_name=agent_name,
                tool_result=result_text,
            )
            return result_text

        if normalized_action == "stop":
            event.set_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY, None)
            result_text = await self._request_stop_background_tasks(event)
            self._queue_call_maid_tool_history(
                event,
                action=normalized_action,
                request_text=request_text,
                agent_name=agent_name,
                tool_result=result_text,
            )
            return result_text

        if normalized_action == "done":
            event.set_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY, None)
            if self.session_store:
                await self.session_store.close_active_session(
                    event.unified_msg_origin,
                    status="done",
                )
            result_text = "当前管家 session 已结束。"
            self._queue_call_maid_tool_history(
                event,
                action=normalized_action,
                request_text=request_text,
                agent_name=agent_name,
                tool_result=result_text,
            )
            return result_text

        if not request_text.strip():
            result_text = "call_maid 需要提供非空的 request_text。"
            self._queue_call_maid_tool_history(
                event,
                action=normalized_action,
                request_text=request_text,
                agent_name=agent_name,
                tool_result=result_text,
            )
            return result_text

        if normalized_action == "steer":
            event.set_extra(PENDING_MAID_DISPATCHES_EXTRA_KEY, None)
            result_text = await self._steer_background_task(event, request_text)
            self._queue_call_maid_tool_history(
                event,
                action=normalized_action,
                request_text=request_text,
                agent_name=agent_name,
                tool_result=result_text,
            )
            return result_text

        resolved_agent_name = self._resolve_allowed_agent_name(
            self.maid_mode_config.allowed_agent_names,
            self.maid_mode_config.default_agent_name,
            agent_name,
        )
        pending_count = self._append_pending_dispatch(
            event,
            agent_name=resolved_agent_name,
            maid_request=request_text,
        )
        if pending_count == 1:
            result_text = (
                f"已记录管家任务请求，当前回复发送后将开始执行。 agent={resolved_agent_name}"
            )
        else:
            result_text = (
                "已记录新的批量管家任务请求，当前回复发送后将合并并发执行。"
                f" 当前批量数={pending_count}"
            )
        self._queue_call_maid_tool_history(
            event,
            action=normalized_action,
            request_text=request_text,
            agent_name=resolved_agent_name,
            tool_result=result_text,
        )
        return result_text

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
        dispatch_error: str | None = None
        final_status = "done"

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
                        (
                            lambda output: self._update_single_assistant_output(task_id, output)
                        )
                        if task_id
                        else None
                    ),
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
                logger.error("[大小姐模式] 子 agent 调度失败: %s", exc, exc_info=True)
                agent_result = f"执行过程中出现问题：{exc!s}"
                resolved_agent_name = agent_name
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
                )
                await self._persist_assistant_reply(
                    event,
                    req,
                    sanitized_follow_up or follow_up_completion_text,
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
                "error"
                if dispatch_error
                else ("stopped" if event.get_extra("agent_stop_requested") else "done")
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
        except Exception as exc:
            final_status = "error"
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
                    event_type="error",
                    title="后台追答任务失败",
                    message=str(exc),
                    source="system",
                    status=final_status,
                )
            logger.error("[大小姐模式] 后台追答任务失败: %s", exc, exc_info=True)

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
        except Exception as exc:
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
                event_type="error",
                title="批量子任务失败",
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
                        meta={"batch_id": batch.batch_id, "session_id": item.session_id},
                    )
                )
                await self._console_event_safe(
                    task_id=item.item_id,
                    event_type="queued",
                    title="批量子任务已排队",
                    message=item.maid_request,
                    source="chat",
                    status=item.status,
                    payload={"batch_id": batch.batch_id, "agent_name": item.agent_name},
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
                )
                await self._persist_assistant_reply(
                    event,
                    req,
                    sanitized_follow_up or follow_up_completion_text,
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
        if sender_id != active_sender_id:
            return "当前后台管家任务不属于本次发言的对方，无法补充要求。"

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
                batch_id = uuid.uuid4().hex
                pending = {
                    "mode": "batch",
                    "batch_id": batch_id,
                    "items": pending_items,
                    "maid_full_reply": maid_full_reply,
                    "true_user_input": (
                        true_user_input if self.maid_mode_config.include_raw_user_input else None
                    ),
                    "image_urls_raw": image_urls_raw,
                    "session_done_requested": False,
                }
                task_info = await self.background_tasks.create_task(
                    unified_msg_origin=event.unified_msg_origin,
                    sender_id=event.get_sender_id(),
                    agent_name="batch",
                    maid_request=self._summarize_batch_request(pending_items),
                    kind="batch",
                    task_id=batch_id,
                )
                pending["batch_id"] = task_info.task_id
                await self._console_ensure_task_safe(
                    self._build_task_patch(
                        task_id=task_info.task_id,
                        kind=task_info.kind,
                        source="chat",
                        unified_msg_origin=task_info.unified_msg_origin,
                        sender_id=task_info.sender_id,
                        agent_name=task_info.agent_name,
                        status=task_info.status,
                        request_text=task_info.maid_request,
                        title=f"批量任务: {len(pending_items)} 项",
                        meta={"item_count": len(pending_items)},
                    )
                )
                await self._console_event_safe(
                    task_id=task_info.task_id,
                    event_type="queued",
                    title="批量任务已排队",
                    message=task_info.maid_request,
                    source="chat",
                    status=task_info.status,
                    payload={"item_count": len(pending_items)},
                )
                task = asyncio.create_task(
                    self._run_maid_batch_background_task(
                        event=event,
                        req=req,
                        pending=pending,
                    )
                )
                self._track_background_task(task)
                logger.debug(
                    "[大小姐模式] 已投递批量后台管家任务，batch_id=%s，主链路不再等待执行完成",
                    task_info.task_id,
                )
                return

            pending = {
                "agent_name": pending_items[0]["agent_name"],
                "maid_full_reply": maid_full_reply,
                "maid_request": pending_items[0]["maid_request"],
                "true_user_input": (
                    true_user_input if self.maid_mode_config.include_raw_user_input else None
                ),
                "image_urls_raw": image_urls_raw,
                "session_done_requested": False,
            }
            task_info = await self.background_tasks.create_task(
                unified_msg_origin=event.unified_msg_origin,
                sender_id=event.get_sender_id(),
                agent_name=pending["agent_name"],
                maid_request=pending["maid_request"],
            )
            pending["task_id"] = task_info.task_id
            await self._console_ensure_task_safe(
                self._build_task_patch(
                    task_id=task_info.task_id,
                    kind=task_info.kind,
                    source="chat",
                    unified_msg_origin=task_info.unified_msg_origin,
                    sender_id=task_info.sender_id,
                    agent_name=task_info.agent_name,
                    status=task_info.status,
                    request_text=task_info.maid_request,
                    title=f"管家任务: {task_info.maid_request[:60]}",
                )
            )
            await self._console_event_safe(
                task_id=task_info.task_id,
                event_type="queued",
                title="管家任务已排队",
                message=task_info.maid_request,
                source="chat",
                status=task_info.status,
                payload={"agent_name": task_info.agent_name},
            )
            task = asyncio.create_task(
                self._run_maid_follow_up_background_task(
                    event=event,
                    req=req,
                    pending=pending,
                )
            )
            self._track_background_task(task)
            logger.debug(
                "[大小姐模式] 已投递后台管家任务，task_id=%s，主链路不再等待执行完成",
                task_info.task_id,
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
        yield event.plain_result(await self._build_background_status_text(event))

    @maid.command("stop")
    async def maid_stop(self, event: AstrMessageEvent):
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
