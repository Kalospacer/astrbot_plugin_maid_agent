"""
大小姐管家模式插件 —— 事件溯源架构重写版（2.0）

后端：RPC 信封 + seq 事件溯源会话日志 +
投影 + events.mux/events.host SSE 流。聊天侧集成（call_maid/maid_task、
命令、通知投递、记忆）在新核心上重实现。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from inspect import isawaitable
from pathlib import Path

from quart import jsonify, make_response, request

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star, StarTools
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
from .harness import contracts as c
from .harness.api import ApiProxy
from .harness.drivers import DriverRegistry
from .harness.hub import StreamHub, sse_frame
from .harness.rpc import (
    client_response_receipt,
    internal_error,
    new_rpc_id,
    parse_client_request,
    server_request,
    server_response,
    server_response_error,
)
from .harness.store import SessionStore
from .katex_fonts import materialize_katex_fonts

__version__ = "2.0.0"

_SETTINGS_SCHEMA_CACHE: dict | None = None


def _load_settings_schema() -> dict:
    global _SETTINGS_SCHEMA_CACHE
    if _SETTINGS_SCHEMA_CACHE is None:
        path = Path(__file__).parent / "_conf_schema.json"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _SETTINGS_SCHEMA_CACHE = json.load(fh)
        except OSError:
            _SETTINGS_SCHEMA_CACHE = {"type": "object", "properties": {}}
    return _SETTINGS_SCHEMA_CACHE


class _ConfigHolder:
    """ApiProxy 的配置面：读 MaidModeConfig、写 AstrBot 插件配置。"""

    def __init__(self, plugin: "MaidAgent"):
        self.plugin = plugin

    def get_config(self) -> dict:
        return asdict(self.plugin.maid_mode_config)

    def settings_schema(self) -> dict:
        return _load_settings_schema()

    def save_config(self, patch: dict) -> dict:
        self.plugin.config.update(patch)
        try:
            self.plugin.config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 配置持久化失败: %s", exc)
        self.plugin.maid_mode_config = load_maid_mode_config(self.plugin.config)
        return self.get_config()

    def default_agent_name(self) -> str:
        return str(self.plugin.maid_mode_config.default_agent_name or "butler")

    def version(self) -> str:
        return __version__


class MaidAgent(Star):
    """大小姐管家模式插件（事件溯源架构）"""

    def __init__(self, context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.maid_mode_config = load_maid_mode_config(self.config)
        self._active_asyncio_tasks: set[asyncio.Task] = set()

        data_root = Path(StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME))
        self.store = SessionStore(data_root)
        self.mux_hub = StreamHub("mux")
        self.host_hub = StreamHub("host")
        self.registry = DriverRegistry(
            self.context, self.store, self.mux_hub, self.host_hub, self.maid_mode_config
        )
        self.registry.on_turn_terminal = self._on_turn_terminal
        self.api = ApiProxy(store=self.store, registry=self.registry, config_holder=_ConfigHolder(self))

    # ================================================================ 生命周期

    async def initialize(self) -> None:
        self._patch_llm_tool_schemas()
        self._register_web_apis()
        self._materialize_katex_fonts()
        self._schedule_retention_cleanup()
        self._maybe_migrate_legacy()
        logger.info(
            "[MaidAgent] 已加载 (%s) | default_agent=%s | fg_timeout=%ss | capacity=%s/%s | retention=%dd",
            __version__,
            self.maid_mode_config.default_agent_name,
            self.maid_mode_config.foreground_timeout_seconds,
            self.maid_mode_config.max_active_per_umo,
            self.maid_mode_config.max_active_global,
            self.maid_mode_config.retention_days,
        )

    async def terminate(self) -> None:
        await self.registry.shutdown()
        tasks = [task for task in self._active_asyncio_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_asyncio_tasks.clear()
        await self.mux_hub.close()
        await self.host_hub.close()

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._active_asyncio_tasks.add(task)
        task.add_done_callback(self._active_asyncio_tasks.discard)

    def _materialize_katex_fonts(self) -> None:
        """从 dashboard dist 物化 KaTeX 字体到插件页 assets/fonts/（失败不阻塞加载）。"""
        try:
            from astrbot.core.dashboard_assets import resolve_dashboard_dist

            dist = resolve_dashboard_dist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 解析 dashboard dist 失败，KaTeX 字体未物化: %s", exc)
            return
        if not dist:
            logger.warning("[maid] dashboard 静态资源不可用，KaTeX 字体未物化（公式退回系统字体）")
            return
        target = Path(__file__).parent / "pages" / "console" / "assets" / "fonts"
        count = materialize_katex_fonts(Path(dist), target)
        if count:
            logger.info("[maid] KaTeX 字体已物化 %d 个（复用 dashboard dist）", count)

    def _schedule_retention_cleanup(self) -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(3600)
                try:
                    running = {sid for sid, d in self.registry.drivers.items() if d.running}
                    removed = self.store.retention_prune(self.maid_mode_config.retention_days)
                    for sid in removed:
                        running.pop(sid, None)
                        self.registry.drivers.pop(sid, None)
                    if removed:
                        logger.info("[maid] retention 清理 %d 个会话", len(removed))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[maid] retention 清理失败: %s", exc)

        self._track_background_task(asyncio.create_task(_loop(), name="maid-retention-loop"))

    def _maybe_migrate_legacy(self) -> None:
        """一次性迁移旧 runtime 数据（存在旧 agents/ 目录且无标记时）。"""
        legacy_root = self.store.root / "agents"
        marker = self.store.root / ".migrated_v2"
        if marker.exists() or not legacy_root.is_dir():
            return
        try:
            from .migrate import migrate_legacy_agents

            report = migrate_legacy_agents(self.store, legacy_root)
            marker.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            logger.info("[maid] 旧数据迁移完成: %s", report)
        except Exception as exc:  # noqa: BLE001
            logger.error("[maid] 旧数据迁移失败（跳过，保留原目录）: %s", exc, exc_info=True)

    # ================================================================ Web API

    def _register_web_apis(self) -> None:
        prefix = f"/{PLUGIN_DATA_DIR_NAME}"
        routes = [
            (f"{prefix}/api/events.mux", self.web_events_mux, ["GET"], "events.mux SSE"),
            (f"{prefix}/api/events.host", self.web_events_host, ["GET"], "events.host SSE"),
            (f"{prefix}/api/respond", self.web_respond, ["POST"], "RPC respond"),
            (f"{prefix}/api/<path:method>", self.web_rpc, ["POST"], "unary RPC"),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)

    async def web_rpc(self, method: str):
        try:
            body_result = request.get_json()
            body = await body_result if isawaitable(body_result) else body_result
            if not isinstance(body, dict):
                body = {}
        except Exception:  # noqa: BLE001
            return jsonify(server_response_error("", internal_error("请求体必须是 JSON。")))
        try:
            rpc_id, method_name, payload = parse_client_request(body)
        except Exception as exc:  # noqa: BLE001
            return jsonify(server_response_error("", internal_error(str(exc))))
        if method_name != method:
            return jsonify(
                server_response_error(rpc_id, internal_error(f"信封 method 与路径不一致: {method_name}"))
            )
        try:
            value = await self.api.dispatch(method_name, payload, rpc_id=rpc_id)
            return jsonify(server_response(rpc_id, value))
        except Exception as exc:  # noqa: BLE001
            from .harness.rpc import RpcError

            if isinstance(exc, RpcError):
                return jsonify(server_response_error(rpc_id, exc))
            logger.error("[maid] RPC %s 失败: %s", method_name, exc, exc_info=True)
            return jsonify(server_response_error(rpc_id, internal_error(str(exc))))

    async def web_respond(self):
        try:
            body_result = request.get_json()
            body = await body_result if isawaitable(body_result) else body_result
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict) or body.get("type") != "client-response":
            return jsonify(client_response_receipt(False, "bad-response"))
        # 本部署没有可应答的服务端请求（approval/question 未实现）
        return jsonify(client_response_receipt(False, "not-pending"))

    async def web_events_mux(self):
        return await self._sse_response(self.mux_hub, self._mux_baselines())

    async def web_events_host(self):
        return await self._sse_response(self.host_hub, iter(()))

    def _mux_baselines(self):
        """订阅建立时的基线帧：attached 会话的 subscribed + 队列快照。"""
        for session_id, driver in list(self.registry.drivers.items()):
            try:
                last_seq = self.store.log(session_id).last_seq
            except Exception:  # noqa: BLE001
                continue
            yield server_request(
                new_rpc_id(), "session/subscribed", c.frame_session_subscribed(session_id, last_seq)
            )
            if driver.inbox:
                items = [
                    {"id": item["id"], "placement": item["placement"], "message": item["message"]}
                    for item in driver.inbox
                ]
                yield server_request(
                    new_rpc_id(), "session/queue", c.frame_session_queue(session_id, items)
                )

    async def _sse_response(self, hub: StreamHub, baselines):
        queue = await hub.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                for frame in baselines:
                    yield sse_frame(frame)
                while True:
                    item = await queue.get()
                    yield sse_frame(item)
            except asyncio.CancelledError:
                pass
            finally:
                await hub.unsubscribe(queue)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        try:
            from starlette.responses import StreamingResponse
        except ImportError:  # Quart 原生旧版 dashboard
            StreamingResponse = None
        if StreamingResponse is not None:
            # 适配层对 starlette Response 原样透传（真流式）；Quart Response 会被
            # get_data() 全量缓冲，无限流的 SSE 永远发不出响应头。
            return StreamingResponse(stream(), headers=headers)
        response = await make_response(stream(), {**headers, "Transfer-Encoding": "chunked"})
        response.timeout = None  # type: ignore[attr-defined]
        return response

    # ================================================================ LLM 工具

    def _patch_llm_tool_schemas(self) -> None:
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
                    "resume_session_id": {"type": "string"},
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
                                "run_in_background": {"type": "boolean", "default": False},
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
                    "action": {"type": "string", "enum": ["status", "result", "stop", "steer"]},
                    "session_id": {"type": "string", "description": "会话（agent）ID"},
                    "task_id": {"type": "string", "description": "任务（turn）ID"},
                    "message": {"type": "string"},
                    "block": {"type": "boolean", "default": True},
                    "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 30000},
                },
                "required": ["action"],
            }

    @staticmethod
    def _json_outcome(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _find_session_for_task(self, task_id: str) -> str:
        if not task_id:
            return ""
        for sid in self.store.list_session_ids():
            log = self.store.log(sid)
            if any(
                e.get("type") == "maid/task" and e.get("data", {}).get("taskId") == task_id
                for e in log.read_events()
            ):
                return sid
        return ""

    def _chat_session_for(self, umo: str, agent_name: str, session_id: str = "") -> object:
        """按 resume_session_id 或 (umo, agentName) 复用/创建会话。"""
        if session_id and self.store.exists(session_id):
            return session_id
        for sid in self.store.list_session_ids():
            meta = self.store.log(sid).load_meta()
            if (
                meta.get("umo") == umo
                and meta.get("agentName") == agent_name
                and meta.get("chatOwned")
            ):
                return sid
        preset = agent_name or self.maid_mode_config.default_agent_name
        log = self.store.create_session(
            agent_preset=preset,
            meta={
                "umo": umo,
                "senderId": "chat",
                "agentName": preset,
                "chatOwned": True,
                "notify": True,
            },
        )
        driver = self.registry.attach(log.session_id)
        driver.umo, driver.agent_name, driver.sender_id = umo, preset, "chat"
        self.registry.publish_host_frame(c.frame_host_session_added(log.session_id, True, agentPreset=preset))
        return log.session_id

    @filter.llm_tool(name=CALL_MAID_TOOL_NAME)
    async def call_maid(
        self,
        event,
        request_text: str = "",
        agent_name: str = "",
        resume_session_id: str = "",
        run_in_background: bool = False,
        tasks: list | None = None,
    ) -> str:
        """调度管家 subagent 执行任务并返回结果。

        Args:
            request_text(string): 自包含的任务请求，交给一个管家 agent 执行。
            agent_name(string): 目标管家名；缺省用插件默认。
            resume_session_id(string): 续聊的会话 ID；新任务留空。
            run_in_background(boolean): true 时后台执行，稍后经通知回灌。
            tasks(list): 批量派发，每项 {request_text, agent_name?, run_in_background?}。
        """
        umo = event.unified_msg_origin
        true_user_input = str(event.get_extra(TRUE_USER_INPUT_EXTRA_KEY) or "")
        batch = []
        if tasks:
            for item in tasks[:5]:
                if isinstance(item, dict) and str(item.get("request_text") or "").strip():
                    batch.append(
                        {
                            "request_text": str(item["request_text"]),
                            "agent_name": str(item.get("agent_name") or agent_name or ""),
                            "run_in_background": bool(item.get("run_in_background", run_in_background)),
                        }
                    )
        if not batch:
            if not str(request_text or "").strip():
                return self._json_outcome({"status": "error", "error": "request_text 不能为空。"})
            batch.append(
                {
                    "request_text": request_text,
                    "agent_name": agent_name,
                    "run_in_background": run_in_background,
                }
            )

        results = []
        for item in batch:
            results.append(await self._dispatch_chat_task(event, umo, true_user_input, item))
        if len(results) == 1:
            return self._json_outcome(results[0])
        return self._json_outcome({"status": "batch", "results": results})

    async def _dispatch_chat_task(self, event, umo: str, true_user_input: str, item: dict) -> dict:
        agent_name = item["agent_name"] or self.maid_mode_config.default_agent_name
        if (
            self.maid_mode_config.allowed_agent_names
            and agent_name not in self.maid_mode_config.allowed_agent_names
        ):
            return {"status": "error", "error": f"agent 不在允许列表: {agent_name}"}
        if not self.registry.capacity_available(umo):
            return {"status": "error", "error": "并发上限已满，稍后再试。"}

        session_id = self._chat_session_for(umo, agent_name, item.get("resume_session_id") or "")
        driver = self.registry.attach(session_id)

        user_input_block = f"【{USER_INPUT_BLOCK_LABEL}】\n{true_user_input}\n\n" if true_user_input.strip() else ""
        maid_request_block = f"【{MISTRESS_REQUEST_BLOCK_LABEL}】\n{item['request_text']}\n\n"
        prompt = render_dispatch_prompt(
            self.maid_mode_config.dispatch_prompt_template,
            user_input_block=user_input_block,
            maid_request_block=maid_request_block,
        )

        task_id = uuid.uuid4().hex
        driver.enqueue(c.user_message([c.text_block(prompt)]))
        async with driver.log.lock:
            driver.log.append("maid/task", {"taskId": task_id}, ignorable=True)
        driver.log.update_meta(activeTaskId=task_id, notified=False)

        if item["run_in_background"]:
            self._track_background_task(
                asyncio.create_task(self._watch_and_notify(driver), name=f"maid-notify-{session_id[:8]}")
            )
            return {"status": "running", "session_id": session_id, "task_id": task_id, "mode": "background"}

        fg_timeout = max(1, min(55, _safe_int(self.maid_mode_config.foreground_timeout_seconds, 50)))
        try:
            result = await driver.wait_next_turn_result(timeout=fg_timeout)
            return {
                "status": result.get("status", "completed"),
                "session_id": session_id,
                "task_id": task_id,
                "mode": "foreground",
                "result": result.get("result", ""),
                "error": result.get("error", ""),
            }
        except asyncio.TimeoutError:
            # 前台超时原地转后台：同一 task 继续跑，稍后经通知回灌
            self._track_background_task(
                asyncio.create_task(self._watch_and_notify(driver), name=f"maid-notify-{session_id[:8]}")
            )
            return {
                "status": "running",
                "session_id": session_id,
                "task_id": task_id,
                "mode": "background",
                "background_reason": "timeout",
            }

    async def _watch_and_notify(self, driver) -> None:
        """后台任务看护：等终态后触发通知投递。"""
        try:
            result = await driver.wait_next_turn_result(timeout=None)
            await self._on_turn_terminal(driver, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 后台任务看护失败: %s", exc)

    async def _on_turn_terminal(self, driver, result: dict) -> None:
        meta = driver.log.load_meta()
        if not meta.get("notify"):
            return
        if meta.get("notified"):
            return
        try:
            await self._notify_main_agent(driver, result)
            driver.log.update_meta(notified=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("[maid] notification 唤醒主 agent 失败: session=%s err=%s", driver.session_id[:8], exc, exc_info=True)

    async def _notify_main_agent(self, driver, result: dict) -> None:
        """Wake the main agent once for one UMO notification snapshot.（移植）"""
        umo = driver.umo
        if not umo:
            return
        from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession as _MS
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        from astrbot.core.agent.tool import ToolSet

        ctx = self.context
        session = _MS.from_str(umo)
        status = result.get("status", "")
        body = result.get("result") or result.get("error") or "(空)"
        summary = f"[管家任务通知]\n- session={driver.session_id[:8]} status={status}\n  {body}"
        notification_id = str(meta_task_id(driver) or driver.session_id)
        extras = {
            MAID_NOTIFICATION_IDS_META_KEY: [notification_id],
            "background_task_results": [
                {
                    "session_id": driver.session_id,
                    "status": status,
                    "result": result.get("result", ""),
                    "error": result.get("error", ""),
                }
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
            return
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
        from .toolset_adapter import _load_provider_settings

        provider_settings = _load_provider_settings(ctx, umo)
        config = MainAgentBuildConfig(
            tool_call_timeout=_safe_int(provider_settings.get("tool_call_timeout", 60), 60),
            streaming_response=False,
            provider_settings=provider_settings,
        )
        result_build = await build_main_agent(event=cron_event, plugin_context=ctx, config=config, req=req)
        if result_build is None:
            return
        runner = result_build.agent_runner
        async for _ in runner.step_until_done(30):
            pass
        llm_resp = runner.get_final_llm_resp()
        history_summary = summary
        if llm_resp is not None and getattr(llm_resp, "completion_text", ""):
            history_summary = f"{summary}\n\n主 Agent 处理结果：{llm_resp.completion_text}"
        await persist_agent_history(
            ctx.conversation_manager,
            event=cron_event,
            req=result_build.provider_request,
            summary_note=history_summary,
        )
        persisted = await ctx.conversation_manager.get_conversation(umo, conversation_id)
        history = json.loads(persisted.history or "[]") if persisted else []
        if history and isinstance(history[-1], dict):
            history[-1][MAID_NOTIFICATION_IDS_META_KEY] = [notification_id]
            history[-1][MAID_NOTIFICATION_ID_META_KEY] = notification_id
            await ctx.conversation_manager.update_conversation(umo, conversation_id, history=history)

    @filter.llm_tool(name=MAID_TASK_TOOL_NAME)
    async def maid_task(
        self,
        event,
        action: str,
        session_id: str = "",
        task_id: str = "",
        message: str = "",
        block: bool = True,
        timeout_ms: int = 30000,
    ) -> str:
        """查询/控制管家任务，对齐 Claude TaskOutput 语义。

        Args:
            action(string): 必填。status/result/stop/steer。
            session_id(string): 会话（agent）ID；steer 时必填。
            task_id(string): 任务（turn）ID；status/result/stop 时填写。
            message(string): steer 的补充要求，必须非空。
            block(boolean): result 是否阻塞等待。默认 true。
            timeout_ms(int): result 阻塞超时毫秒。默认 30000，最大 600000。
        """
        normalized = (action or "").strip().casefold()
        sid = session_id or self._find_session_for_task(task_id)
        if not sid or not self.store.exists(sid):
            return self._json_outcome({"status": "error", "error": "找不到对应的会话或任务。"})
        driver = self.registry.attach(sid)

        if normalized == "steer":
            if not message.strip():
                return self._json_outcome({"status": "error", "error": "steer 需要非空 message。"})
            if not driver.running:
                return self._json_outcome({"status": "error", "error": "会话当前没有运行中的任务。"})
            ticket = driver.steer(message)
            return self._json_outcome({"session_id": sid, "status": "steered", "ticket": ticket or ""})

        if normalized == "stop":
            if not driver.running:
                return self._json_outcome({"status": "completed", "session_id": sid, "result": "本就没有运行中的任务。"})
            driver.request_stop()
            return self._json_outcome({"session_id": sid, "status": "stopping"})

        if normalized in {"status", "result"}:
            if driver.running:
                if normalized == "status" or not block:
                    return self._json_outcome({"session_id": sid, "status": "running"})
                timeout = min(600000, max(0, _safe_int(timeout_ms, 30000))) / 1000
                try:
                    result = await driver.wait_next_turn_result(timeout=timeout)
                except asyncio.TimeoutError:
                    return self._json_outcome({"session_id": sid, "status": "running", "query_status": "timeout"})
                driver.log.update_meta(notified=True)  # 主动读取即认领
                return self._json_outcome({"session_id": sid, **result})
            meta = driver.log.load_meta()
            driver.log.update_meta(notified=True)
            return self._json_outcome(
                {
                    "session_id": sid,
                    "status": meta.get("lastStatus", "completed"),
                    "result": meta.get("lastResult", ""),
                    "error": meta.get("lastError", ""),
                }
            )

        return self._json_outcome({"status": "error", "error": f"maid_task action 非法: {action}"})

    # ================================================================ 命令与钩子

    @filter.command_group("maid")
    def maid(self):
        pass

    @maid.command("status")
    async def maid_status(self, event):
        active = [
            d
            for sid, d in self.registry.drivers.items()
            if d.running and d.umo == event.unified_msg_origin
        ]
        if active:
            lines = ["当前活跃管家会话："]
            lines.extend(f"- session={d.session_id[:8]} umo={d.umo}" for d in active)
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result("当前会话没有运行中的管家任务。")

    @maid.command("stop")
    async def maid_stop(self, event):
        active = [
            d
            for sid, d in self.registry.drivers.items()
            if d.running and d.umo == event.unified_msg_origin
        ]
        for driver in active:
            driver.request_stop()
        yield event.plain_result(f"已请求停止 {len(active)} 个活跃任务。")

    @filter.on_decorating_result()
    async def stash_raw_input(self, event) -> None:
        raw_input = event.message_str
        if raw_input:
            event.set_extra(RAW_INPUT_EXTRA_KEY, raw_input[:2000])
            event.set_extra(TRUE_USER_INPUT_EXTRA_KEY, raw_input)


def meta_task_id(driver) -> str:
    meta = driver.log.load_meta()
    return str(meta.get("activeTaskId") or "")
