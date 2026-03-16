"""
大小姐管家模式插件

实现主对话模型与执行代理的角色分离：
- 主模型（大小姐）仅保留自然语言对话上下文
- 主模型不直接暴露任何原生工具
- 需要幕后执行时通过 `<call_maid>` XML 协议表达意图
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star, register
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
from .config import load_maid_mode_config
from .constants import (
    INTERNAL_SEND_KIND_EXTRA_KEY,
    PENDING_MAID_FOLLOW_UP_EXTRA_KEY,
    RAW_INPUT_EXTRA_KEY,
    SERVING_ENABLED_KEY_PREFIX,
    TRUE_USER_INPUT_EXTRA_KEY,
)
from .context_sanitizer import sanitize_contexts
from .maid_call_parser import parse_maid_call
from .maid_dispatcher import dispatch_to_maid_agent
from .output_sanitizer import sanitize_user_visible_output
from .prompt_injector import inject_maid_system_prompt
from .response_validator import validate_llm_response
from .session_store import MaidSessionStore

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context


@register(
    "MaidAgent",
    "大小姐管家模式",
    "主模型仅保留自然语言上下文，通过 XML 协议请求幕后执行",
    "1.0.0",
)
class MaidAgent(Star):
    """大小姐管家模式插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.maid_mode_config = load_maid_mode_config(self.config)
        self.session_store: MaidSessionStore | None = None
        self.background_tasks = MaidBackgroundTaskRegistry()
        self._active_asyncio_tasks: set[asyncio.Task] = set()
        self._background_runners_by_umo: dict[str, object] = {}
        self._active_self_serving_tasks_by_umo: dict[str, asyncio.Task] = {}

    async def initialize(self) -> None:
        """插件初始化"""
        self.session_store = MaidSessionStore(self, self.maid_mode_config)
        logger.info(
            "[MaidAgent] 已加载 | default_agent=%s | allowed_agents=%s | call_tag=%s | include_raw_user_input=%s | session_enabled=%s | log_raw_llm_io=%s | session_timeout_minutes=%s",
            self.maid_mode_config.default_agent_name,
            ",".join(self.maid_mode_config.allowed_agent_names or []),
            self.maid_mode_config.call_tag_name,
            self.maid_mode_config.include_raw_user_input,
            self.maid_mode_config.session_enabled,
            self.maid_mode_config.log_raw_llm_io,
            self.maid_mode_config.session_timeout_minutes,
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
    def _set_internal_send_kind(event: AstrMessageEvent, kind: str | None) -> None:
        event.set_extra(INTERNAL_SEND_KIND_EXTRA_KEY, kind)

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._active_asyncio_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._active_asyncio_tasks.discard(done_task)
            try:
                done_task.result()
            except Exception as exc:
                logger.error("[大小姐模式] 后台任务异常退出: %s", exc, exc_info=True)

        task.add_done_callback(_on_done)

    @filter.on_llm_request()
    async def sanitize_main_model_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """
        清洗主模型请求，实现大小姐模式。

        1. 保存原始对话输入到 event.extra
        2. 清洗 contexts - 过滤 tool role 和 tool_calls
        3. 禁用主模型原生工具
        4. 注入大小姐 XML 协议说明
        """
        raw_input = req.prompt or event.message_str or ""
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input)
            logger.debug(f"[大小姐模式] 已保存原始输入: {raw_input[:100]}...")

        true_user_input = event.message_str or ""
        if true_user_input:
            event.set_extra(TRUE_USER_INPUT_EXTRA_KEY, true_user_input)
            logger.debug(f"[大小姐模式] 已保存真实用户文本: {true_user_input[:100]}...")

        removed_count = sanitize_contexts(req)
        if removed_count > 0:
            logger.debug(f"[大小姐模式] 已清洗 contexts，移除 {removed_count} 条非自然语言消息")

        req.func_tool = ToolSet()
        logger.debug("[大小姐模式] 已注入空工具集，禁用主模型原生工具暴露")

        if inject_maid_system_prompt(
            req,
            self.maid_mode_config.call_tag_name,
            self.maid_mode_config.default_agent_name,
            self.maid_mode_config.main_system_prompt_template,
        ):
            logger.debug("[大小姐模式] 已注入 XML 调度协议说明")

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
                            name=f"transfer_to_{agent_name}",
                            arguments=json.dumps({"input": maid_request}, ensure_ascii=False),
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

    async def _request_serving_follow_up(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        contexts: list[dict],
    ) -> LLMResponse:
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到用于服侍模式追答的 provider: {provider_id}")
        return await provider.text_chat(
            prompt=self.maid_mode_config.serving_prompt_template,
            image_urls=None,
            func_tool=ToolSet(),
            contexts=contexts,
            system_prompt=req.system_prompt,
            model=req.model,
        )

    async def _build_serving_contexts(
        self,
        req: ProviderRequest,
        latest_assistant_text: str,
    ) -> list[dict]:
        contexts: list[dict] = []
        for msg in req.contexts or []:
            if isinstance(msg, dict):
                contexts.append(dict(msg))
            elif hasattr(msg, "model_dump"):
                contexts.append(msg.model_dump())
        if req.prompt is not None:
            assembled = await req.assemble_context()
            if isinstance(assembled, dict):
                contexts.append(assembled)
            elif hasattr(assembled, "model_dump"):
                contexts.append(assembled.model_dump())
        if latest_assistant_text.strip():
            contexts.append({"role": "assistant", "content": latest_assistant_text.strip()})
        return contexts

    async def _run_self_serving_background_task(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        latest_assistant_text: str,
    ) -> None:
        self._active_self_serving_tasks_by_umo[event.unified_msg_origin] = asyncio.current_task()
        try:
            contexts = await self._build_serving_contexts(req, latest_assistant_text)
            budget = self.maid_mode_config.serving_max_turns
            while budget > 0:
                if event.is_stopped() or bool(event.get_extra("agent_stop_requested")):
                    return
                follow_up_resp = await self._request_serving_follow_up(
                    event=event,
                    req=req,
                    contexts=contexts,
                )
                completion_text = follow_up_resp.completion_text or ""
                sanitized = sanitize_user_visible_output(
                    completion_text,
                    self.maid_mode_config.call_tag_name,
                )
                if sanitized != completion_text:
                    self._rewrite_response_text(follow_up_resp, sanitized)

                if follow_up_resp.result_chain is not None or sanitized.strip():
                    chain = follow_up_resp.result_chain or MessageChain(
                        chain=[Comp.Plain(sanitized)]
                    )
                    self._set_internal_send_kind(event, "self_serving")
                    await event.send(chain)
                    self._set_internal_send_kind(event, None)
                    latest_assistant_text = sanitized or completion_text

                contexts.append(
                    {"role": "user", "content": self.maid_mode_config.serving_prompt_template}
                )
                contexts.append({"role": "assistant", "content": sanitized or completion_text})
                budget -= 1
                if not sanitized.strip():
                    break
        except Exception as exc:
            logger.error("[大小姐模式] 服侍模式自动连发失败: %s", exc, exc_info=True)
        finally:
            self._set_internal_send_kind(event, None)
            current = self._active_self_serving_tasks_by_umo.get(event.unified_msg_origin)
            if current is asyncio.current_task():
                self._active_self_serving_tasks_by_umo.pop(event.unified_msg_origin, None)

    async def _run_maid_follow_up_background_task(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        pending: dict,
    ) -> None:
        cfg = self.maid_mode_config
        task_id = str(pending.get("task_id") or "")
        agent_name = pending.get("agent_name") or cfg.default_agent_name
        maid_full_reply = pending.get("maid_full_reply") or ""
        maid_request = pending.get("maid_request") or ""
        true_user_input = pending.get("true_user_input")
        image_urls_raw = pending.get("image_urls_raw")
        session_done_requested = bool(pending.get("session_done_requested", False))
        reasoning_content = str(pending.get("reasoning_content", "") or "")
        reasoning_signature = pending.get("reasoning_signature")
        dispatch_error: str | None = None

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
                logger.error(f"[大小姐模式] 子 agent 调度失败: {exc!s}", exc_info=True)
                agent_result = f"执行过程中出现问题：{exc!s}"
                resolved_agent_name = agent_name
                dispatch_error = str(exc)

            maid_visible_text = sanitize_user_visible_output(
                maid_full_reply,
                cfg.call_tag_name,
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
            follow_up_call = parse_maid_call(
                follow_up_completion_text,
                cfg.call_tag_name,
            )
            follow_up_done_requested = bool(follow_up_call and follow_up_call.action == "done")
            sanitized_follow_up = sanitize_user_visible_output(
                follow_up_completion_text,
                cfg.call_tag_name,
            )
            if sanitized_follow_up != follow_up_completion_text:
                self._rewrite_response_text(follow_up_resp, sanitized_follow_up)

            if follow_up_resp.result_chain is not None or sanitized_follow_up.strip():
                chain = follow_up_resp.result_chain or MessageChain(
                    chain=[Comp.Plain(sanitized_follow_up)]
                )
                self._set_internal_send_kind(event, "maid_follow_up")
                await event.send(chain)
                self._set_internal_send_kind(event, None)

            if (session_done_requested or follow_up_done_requested) and self.session_store:
                await self.session_store.close_active_session(
                    event.unified_msg_origin, status="done"
                )
            if task_id:
                final_status = (
                    "error"
                    if dispatch_error
                    else ("stopped" if event.get_extra("agent_stop_requested") else "done")
                )
                await self.background_tasks.finish(
                    task_id,
                    status=final_status,
                    result=sanitized_follow_up or agent_result,
                    error=dispatch_error or "",
                )
        except Exception as exc:
            if task_id:
                await self.background_tasks.finish(
                    task_id,
                    status="error",
                    error=str(exc),
                )
            logger.error("[大小姐模式] 后台追答任务失败: %s", exc, exc_info=True)
        finally:
            self._set_internal_send_kind(event, None)

    async def _build_background_status_text(self, event: AstrMessageEvent) -> str:
        current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        active = await self.background_tasks.list_active()
        lines = [f"当前后台管家任务数: {len(active)}"]
        if current is None:
            lines.append("当前会话没有运行中的管家任务。")
            return "\n".join(lines)

        lines.append(f"当前任务ID: {current.task_id}")
        lines.append(f"状态: {current.status}")
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

        stopped = active_event_registry.request_agent_stop_all(
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

    def _serving_enabled_key(self, event: AstrMessageEvent) -> str:
        return f"{SERVING_ENABLED_KEY_PREFIX}{event.unified_msg_origin}"

    async def _get_serving_enabled(self, event: AstrMessageEvent) -> bool:
        if not self.maid_mode_config.serving_mode_enabled:
            return False
        stored = await self.get_kv_data(self._serving_enabled_key(event), None)
        return bool(stored)

    async def _set_serving_enabled(self, event: AstrMessageEvent, enabled: bool) -> None:
        await self.put_kv_data(self._serving_enabled_key(event), enabled)

    async def _toggle_serving_enabled(self, event: AstrMessageEvent) -> str:
        if not self.maid_mode_config.serving_mode_enabled:
            return "服侍模式全局开关当前已关闭，无法在会话中启用。"
        enabled = not await self._get_serving_enabled(event)
        await self._set_serving_enabled(event, enabled)
        return f"当前会话服侍模式已{'开启' if enabled else '关闭'}。"

    async def _steer_background_task(
        self,
        event: AstrMessageEvent,
        message_text: str,
    ) -> str:
        current = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        if current is None:
            return "当前会话没有运行中的管家任务，无法补充要求。"

        runner = self._background_runners_by_umo.get(event.unified_msg_origin)
        if runner is None:
            return "当前没有可引导的活跃管家执行器，请稍后再试。"

        runner_event = getattr(getattr(runner.run_context, "context", None), "event", None)
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

    def _unregister_background_runner(self, umo: str, runner: object) -> None:
        if self._background_runners_by_umo.get(umo) is runner:
            self._background_runners_by_umo.pop(umo, None)

    @filter.on_llm_response()
    async def sanitize_llm_response(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """
        解析 XML 调度标签，并在本阶段打通子 agent 调度与回灌闭环。
        """
        native_tools = validate_llm_response(resp)
        if native_tools:
            logger.debug(f"[大小姐模式] 观测到主模型残留原生工具调用倾向: {native_tools}")

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

        cfg = self.maid_mode_config
        completion_text = resp.completion_text or ""
        maid_call = parse_maid_call(completion_text, cfg.call_tag_name)
        session_done_requested = bool(maid_call and maid_call.action == "done")
        if session_done_requested and self.session_store:
            await self.session_store.close_active_session(event.unified_msg_origin, status="done")
            sanitized = sanitize_user_visible_output(
                completion_text,
                cfg.call_tag_name,
            )
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            return

        if maid_call and maid_call.action in {"stop", "steer"}:
            sanitized = sanitize_user_visible_output(
                completion_text,
                cfg.call_tag_name,
            )
            if maid_call.action == "stop":
                control_text = await self._request_stop_background_tasks(event)
            elif maid_call.action == "steer":
                control_text = await self._steer_background_task(
                    event,
                    maid_call.request_text,
                )
            else:
                control_text = ""
            final_text = control_text
            if sanitized:
                final_text = f"{sanitized}\n\n{control_text}".strip()
            self._rewrite_response_text(resp, final_text or sanitized)
            return

        if not maid_call:
            sanitized = sanitize_user_visible_output(
                completion_text,
                cfg.call_tag_name,
            )
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            return

        agent_name = maid_call.agent_name or cfg.default_agent_name
        if cfg.allowed_agent_names and not self._contains_agent_name(
            cfg.allowed_agent_names, agent_name
        ):
            logger.warning(f"[大小姐模式] XML 请求的目标 agent 不在白名单中: {agent_name}")
            agent_name = cfg.default_agent_name

        true_user_input = event.get_extra(TRUE_USER_INPUT_EXTRA_KEY, "") or ""
        image_urls_raw = getattr(getattr(event, "message_obj", None), "image_urls", None)

        logger.debug(
            f"[大小姐模式] 检测到 <{cfg.call_tag_name}>，目标 agent={agent_name}，"
            f"请求摘要: {maid_call.request_text[:100]}..."
        )

        req = event.get_extra("provider_request")
        if not self._is_provider_request_like(req):
            sanitized = sanitize_user_visible_output(
                completion_text,
                cfg.call_tag_name,
            )
            if sanitized != completion_text:
                self._rewrite_response_text(resp, sanitized)
            logger.error(
                "[大小姐模式] event.extra['provider_request'] 不存在或类型错误: type=%s missing=%s",
                type(req).__name__ if req is not None else "NoneType",
                self._get_missing_provider_request_attrs(req),
            )
            return

        maid_visible_text = sanitize_user_visible_output(
            completion_text,
            cfg.call_tag_name,
        )
        if maid_visible_text != completion_text and maid_visible_text.strip():
            self._rewrite_response_text(resp, maid_visible_text)
        elif not maid_visible_text.strip():
            resp.result_chain = None
            resp.completion_text = ""
            resp.tools_call_name = []
            resp.tools_call_args = []
            resp.tools_call_ids = []
            resp.tools_call_extra_content = {}
        event.set_extra(
            PENDING_MAID_FOLLOW_UP_EXTRA_KEY,
            {
                "agent_name": agent_name,
                "maid_full_reply": completion_text,
                "maid_request": maid_call.request_text,
                "true_user_input": true_user_input if cfg.include_raw_user_input else None,
                "image_urls_raw": image_urls_raw,
                "session_done_requested": session_done_requested,
                "reasoning_content": resp.reasoning_content,
                "reasoning_signature": resp.reasoning_signature,
            },
        )
        if maid_visible_text.strip():
            logger.debug("[大小姐模式] 已保留第一条大小姐回复，并挂起管家后续处理")
            return

        current_task = await self.background_tasks.get_active_by_umo(event.unified_msg_origin)
        if current_task is not None:
            logger.warning(
                "[大小姐模式] 当前会话已有后台任务运行，将纯协议 call_maid 降级为补充要求: current_task_id=%s requested_agent=%s",
                current_task.task_id,
                agent_name,
            )
            steer_text = await self._steer_background_task(event, maid_call.request_text)
            if steer_text.strip():
                self._rewrite_response_text(resp, steer_text)
            else:
                resp.result_chain = None
                resp.completion_text = ""
                resp.tools_call_name = []
                resp.tools_call_args = []
                resp.tools_call_ids = []
                resp.tools_call_extra_content = {}
            self._clear_pending_follow_up(event)
            return

        logger.debug("[大小姐模式] 首条回复仅含协议标签，直接投递后台管家任务")
        task_info = await self.background_tasks.create_task(
            unified_msg_origin=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            agent_name=agent_name,
            maid_request=maid_call.request_text,
        )
        immediate_pending = dict(event.get_extra(PENDING_MAID_FOLLOW_UP_EXTRA_KEY) or {})
        immediate_pending["task_id"] = task_info.task_id
        self._clear_pending_follow_up(event)
        task = asyncio.create_task(
            self._run_maid_follow_up_background_task(
                event=event,
                req=req,
                pending=immediate_pending,
            )
        )
        self._track_background_task(task)
        resp.result_chain = None
        resp.completion_text = ""
        resp.tools_call_name = []
        resp.tools_call_args = []
        resp.tools_call_ids = []
        resp.tools_call_extra_content = {}
        return

    @filter.after_message_sent()
    async def continue_maid_follow_up_after_send(self, event: AstrMessageEvent) -> None:
        pending = event.get_extra(PENDING_MAID_FOLLOW_UP_EXTRA_KEY)
        try:
            req = event.get_extra("provider_request")
            if not self._is_provider_request_like(req):
                if isinstance(pending, dict) or await self._get_serving_enabled(event):
                    logger.error(
                        "[大小姐模式] after_message_sent 阶段 provider_request 不存在或类型错误: type=%s missing=%s",
                        type(req).__name__ if req is not None else "NoneType",
                        self._get_missing_provider_request_attrs(req),
                    )
                return

            if isinstance(pending, dict):
                current_task = await self.background_tasks.get_active_by_umo(
                    event.unified_msg_origin
                )
                if current_task is not None:
                    logger.warning(
                        "[大小姐模式] 当前会话已有后台任务运行，将新的 call_maid 降级为补充要求: current_task_id=%s requested_agent=%s",
                        current_task.task_id,
                        pending.get("agent_name") or self.maid_mode_config.default_agent_name,
                    )
                    maid_request = str(pending.get("maid_request") or "")
                    self._clear_pending_follow_up(event)
                    steer_text = await self._steer_background_task(event, maid_request)
                    if steer_text.strip():
                        await event.send(MessageChain(chain=[Comp.Plain(steer_text)]))
                    return
                agent_name = pending.get("agent_name") or self.maid_mode_config.default_agent_name
                maid_request = pending.get("maid_request") or ""
                task_info = await self.background_tasks.create_task(
                    unified_msg_origin=event.unified_msg_origin,
                    sender_id=event.get_sender_id(),
                    agent_name=agent_name,
                    maid_request=maid_request,
                )
                pending = dict(pending)
                pending["task_id"] = task_info.task_id
                self._clear_pending_follow_up(event)
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
            internal_send_kind = event.get_extra(INTERNAL_SEND_KIND_EXTRA_KEY)
            if internal_send_kind:
                return
            if not await self._get_serving_enabled(event):
                return
            if event.unified_msg_origin in self._active_self_serving_tasks_by_umo:
                logger.debug("[大小姐模式] 当前会话已有服侍模式自动连发任务，跳过重复投递")
                return

            latest_assistant_text = ""
            result = getattr(event, "result", None)
            chain = getattr(result, "chain", None)
            if chain is not None and hasattr(chain, "get_plain_text"):
                latest_assistant_text = chain.get_plain_text() or ""
            if not latest_assistant_text.strip():
                return

            task = asyncio.create_task(
                self._run_self_serving_background_task(
                    event=event,
                    req=req,
                    latest_assistant_text=latest_assistant_text,
                )
            )
            self._track_background_task(task)
            logger.debug("[大小姐模式] 已投递服侍模式自动连发任务")
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

    @filter.command("maid_serve")
    async def maid_serve_toggle(self, event: AstrMessageEvent):
        yield event.plain_result(await self._toggle_serving_enabled(event))

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent) -> None:
        """
        结果装饰阶段。

        当前仅用于日志记录；最终对外输出清洗已在响应阶段执行。
        """
        raw_input = event.get_extra(RAW_INPUT_EXTRA_KEY)
        if raw_input:
            logger.debug(f"[大小姐模式] 本轮对话原始输入: {raw_input[:100]}...")
