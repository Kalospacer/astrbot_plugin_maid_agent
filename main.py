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
from typing import TYPE_CHECKING

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
from astrbot.core.provider.entities import ToolCallsResult
from astrbot.core.utils.active_event_registry import active_event_registry

from .background_registry import MaidBackgroundTaskRegistry
from .batch_registry import MaidBatchRegistry
from .config import load_maid_mode_config
from .constants import (
    CALL_MAID_TOOL_NAME,
    PENDING_MAID_DISPATCHES_EXTRA_KEY,
    PENDING_MAID_FOLLOW_UP_EXTRA_KEY,
    PENDING_MAID_TOOL_HISTORY_EXTRA_KEY,
    RAW_INPUT_EXTRA_KEY,
    TRUE_USER_INPUT_EXTRA_KEY,
)
from .maid_dispatcher import dispatch_to_maid_agent
from .session_store import MaidSessionStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context


class MaidAgent(Star):
    """大小姐管家模式插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.maid_mode_config = load_maid_mode_config(self.config)
        self.session_store: MaidSessionStore | None = None
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
                action = str(record.get("action") or "").strip()
                request_text = str(record.get("request_text") or "")
                agent_name = str(record.get("agent_name") or "")
                tool_result = str(record.get("tool_result") or "")
                tool_call_id = f"maid_hist_{uuid.uuid4().hex}"

                arguments = {"action": action}
                if request_text.strip():
                    arguments["request_text"] = request_text
                if agent_name.strip():
                    arguments["agent_name"] = agent_name

                tool_history_messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "type": "function",
                                "id": tool_call_id,
                                "function": {
                                    "name": CALL_MAID_TOOL_NAME,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
                tool_history_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result,
                    }
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
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到用于大小姐追答的 provider: {provider_id}")

        tool_call_id = f"maid_{uuid.uuid4().hex}"
        assistant_parts = []
        if reasoning_content or reasoning_signature:
            assistant_parts.append(
                ThinkPart(
                    think=reasoning_content or "",
                    encrypted=reasoning_signature,
                )
            )
        if maid_visible_text.strip():
            assistant_parts.append(TextPart(text=maid_visible_text))
        if not assistant_parts:
            assistant_parts = None
        tool_calls_result = ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                content=assistant_parts,
                tool_calls=[
                    ToolCall(
                        id=tool_call_id,
                        function=ToolCall.FunctionBody(
                            name=CALL_MAID_TOOL_NAME,
                            arguments=json.dumps(
                                {
                                    "action": "dispatch",
                                    "agent_name": agent_name,
                                    "request_text": maid_request,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                ],
            ),
            tool_calls_result=[
                ToolCallMessageSegment(
                    tool_call_id=tool_call_id,
                    content=agent_result,
                )
            ],
        )
        return await provider.text_chat(
            prompt=req.prompt,
            image_urls=req.image_urls,
            func_tool=None,
            contexts=req.contexts,
            system_prompt=req.system_prompt,
            tool_calls_result=tool_calls_result,
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
                            lambda output: self.background_tasks.update_assistant_output(
                                task_id, output
                            )
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
            except Exception as exc:
                logger.error("[大小姐模式] 子 agent 调度失败: %s", exc, exc_info=True)
                agent_result = f"执行过程中出现问题：{exc!s}"
                resolved_agent_name = agent_name
                dispatch_error = str(exc)

            maid_visible_text = maid_full_reply.strip()
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
            if self.session_store:
                await self.session_store.close_session(session_id, status=final_status)
            return

        await self.batch_registry.update_item_running(batch_id, item_id)
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
                    lambda output: self.batch_registry.update_item_assistant_output(
                        batch_id,
                        item_id,
                        output,
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

            await self.background_tasks.finish(
                batch_id,
                status=batch.status,
                result=sanitized_follow_up or batch_result,
            )
        except Exception as exc:
            await self.background_tasks.finish(
                batch_id,
                status="error",
                error=str(exc),
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

    async def _request_stop_background_tasks(self, event: AstrMessageEvent) -> str:
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
        lines = [f"已请求停止当前会话的后台管家任务。task_id={current.task_id}"]
        lines.append(f"命中的活跃事件数: {stopped}")
        lines.append("如果子 agent 正在等待工具或网络返回，通常会在当前步骤结束后停止。")
        return "\n".join(lines)

    async def _steer_background_task(
        self,
        event: AstrMessageEvent,
        message_text: str,
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
