"""大小姐管家模式插件：RPC 信封 + seq 事件溯源会话日志 +
投影 + events.mux/events.host SSE 流。聊天侧集成五个 maid 控制工具、
命令、通知投递与记忆。
"""

from __future__ import annotations

import asyncio
import json
import time
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
    MAID_AGENT_TOOL_NAME,
    MAID_LIST_AGENTS_TOOL_NAME,
    MAID_SEND_MESSAGE_TOOL_NAME,
    MAID_TASK_OUTPUT_TOOL_NAME,
    MAID_TASK_STOP_TOOL_NAME,
    MAID_NOTIFICATION_ID_META_KEY,
    MAID_NOTIFICATION_IDS_META_KEY,
    PLUGIN_DATA_DIR_NAME,
    RAW_INPUT_EXTRA_KEY,
    TRUE_USER_INPUT_EXTRA_KEY,
)
from .harness import contracts as c
from .harness._log import dump_raw_llm_output, dump_raw_llm_request
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
from .toolset_adapter import apply_main_tool_policy

__version__ = "2.0.5"

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
        return {"type": "object", "properties": _load_settings_schema()}

    def save_config(self, patch: dict) -> dict:
        effective = load_maid_mode_config(self.plugin.config, strict=False)
        candidate = asdict(effective)
        candidate.update(patch)
        validated = load_maid_mode_config(candidate)
        self.plugin.config.clear()
        self.plugin.config.update(candidate)
        try:
            self.plugin.config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 配置持久化失败: %s", exc)
        self.plugin.maid_mode_config = validated
        self.plugin.registry.config = self.plugin.maid_mode_config
        return self.get_config()

    def version(self) -> str:
        return __version__


class MaidAgent(Star):
    """大小姐管家模式插件。"""

    def __init__(self, context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.maid_mode_config = load_maid_mode_config(self.config, strict=False)
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


    async def initialize(self) -> None:
        self._patch_llm_tool_schemas()
        self._register_web_apis()
        self._materialize_katex_fonts()
        self._schedule_retention_cleanup()
        self._schedule_turn_watchdog()
        logger.info(
            "[MaidAgent] 已加载 (%s) | dispatch_mode=%s | capacity=%s/%s | retention=%dd",
            __version__,
            self.maid_mode_config.dispatch_session_mode,
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

    def _schedule_turn_watchdog(self) -> None:
        """turn 看门狗：超时强制终止挂死的 turn，防止 webui 永远显示工作中。"""

        async def _loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    limit = float(self.maid_mode_config.max_turn_seconds or 0)
                    if limit <= 0:
                        continue
                    now = time.monotonic()
                    for driver in list(self.registry.drivers.values()):
                        started = driver.turn_started_at
                        if driver.running and started is not None and now - started > limit:
                            logger.warning(
                                "[maid] 看门狗终止超时 turn (%.0fs): session=%s",
                                now - started,
                                driver.session_id[:8],
                            )
                            driver.watchdog_cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[maid] turn 看门狗失败: %s", exc)

        self._track_background_task(asyncio.create_task(_loop(), name="maid-turn-watchdog"))

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
        except ImportError:
            StreamingResponse = None
        if StreamingResponse is not None:
            return StreamingResponse(stream(), headers=headers)
        response = await make_response(stream(), {**headers, "Transfer-Encoding": "chunked"})
        response.timeout = None  # type: ignore[attr-defined]
        return response


    def _patch_llm_tool_schemas(self) -> None:
        manager = self.context.get_llm_tool_manager()
        agent_tool = manager.get_func(MAID_AGENT_TOOL_NAME) if manager else None
        if agent_tool is not None:
            agent_tool.parameters = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Self-contained task request for one agent.",
                    },
                    "subagent_type": {"type": "string", "description": "Configured subagent name."},
                    "run_in_background": {"type": "boolean", "default": False},
                    "resume_agent_id": {"type": "string", "description": "Existing maid agent ID to continue."},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "prompt": {"type": "string", "minLength": 1},
                                "subagent_type": {"type": "string", "minLength": 1},
                                "run_in_background": {"type": "boolean", "default": False},
                            },
                            "required": ["prompt", "subagent_type"],
                        },
                    },
                },
                "required": ["prompt", "subagent_type"],
            }
        send_tool = manager.get_func(MAID_SEND_MESSAGE_TOOL_NAME) if manager else None
        if send_tool is not None:
            send_tool.parameters = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "agent_id": {"type": "string"},
                    "message": {"type": "string", "minLength": 1},
                },
                "required": ["agent_id", "message"],
            }
        output_tool = manager.get_func(MAID_TASK_OUTPUT_TOOL_NAME) if manager else None
        if output_tool is not None:
            output_tool.parameters = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "task_id": {"type": "string"},
                    "block": {"type": "boolean", "default": True},
                    "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 30000},
                },
                "required": ["task_id"],
            }
        stop_tool = manager.get_func(MAID_TASK_STOP_TOOL_NAME) if manager else None
        if stop_tool is not None:
            stop_tool.parameters = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            }

    @filter.on_llm_request()
    async def _on_main_llm_request(self, _event, req) -> None:
        """主模型（大小姐）请求的 maid 模式处理：工具可见性策略 + 原始请求 dump。

        maid 子代理直接走 ``ToolLoopAgentRunner``（``maid_dispatcher._build_runner``），
        不经过 ``OnLLMRequestEvent`` 管道钩子，因此这里只影响主模型的请求，不会剥掉
        maid 自己的工具。dump 放在策略之后，日志里看到的就是过滤后的工具列表。
        配置实时读取：改配置后下一次请求立即生效，无需重启。
        """
        cfg = self.maid_mode_config
        if req.func_tool is not None and (cfg.hide_native_tools or cfg.hide_transfer_tools):
            apply_main_tool_policy(
                req.func_tool,
                hide_native_tools=cfg.hide_native_tools,
                hide_transfer_tools=cfg.hide_transfer_tools,
            )
        if cfg.log_raw_llm_io:
            dump_raw_llm_request(req, source="main")

    @filter.on_llm_response()
    async def _log_main_llm_response(self, _event, resp) -> None:
        """log_raw_llm_io 开启时 dump 主模型最终输出（请求侧见 ``_apply_main_tool_policy``）。"""
        if self.maid_mode_config.log_raw_llm_io:
            dump_raw_llm_output(getattr(resp, "completion_text", "") or "", source="main")

    @staticmethod
    def _json_outcome(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _find_agent_for_task(self, task_id: str) -> str:
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

    def _create_chat_agent(self, umo: str, subagent_type: str, *, execution_mode: str, dispatch_id: str, background_reason: str = "") -> str:
        log = self.store.create_session(
            agent_preset=subagent_type,
            meta={
                "umo": umo,
                "senderId": "chat",
                "agentName": subagent_type,
                "agentId": "",
                "dispatchId": dispatch_id,
                "executionMode": execution_mode,
                "sourceKind": "chat",
                "backgroundReason": background_reason,
                "chatOwned": True,
                "notify": execution_mode == "background",
                "foregroundLease": dispatch_id if execution_mode == "foreground" else None,
                "deliveryStatus": "pending" if execution_mode == "background" else "skipped",
            },
        )
        log.update_meta(agentId=log.session_id)
        driver = self.registry.attach(log.session_id)
        driver.umo, driver.agent_name, driver.sender_id = umo, subagent_type, "chat"
        self.registry.publish_host_frame(
            c.frame_host_session_added(
                log.session_id, True, agentPreset=subagent_type, executionMode=execution_mode,
                sourceKind="chat", dispatchId=dispatch_id, backgroundReason=background_reason or None,
                foregroundLease=dispatch_id if execution_mode == "foreground" else None,
            )
        )
        return log.session_id

    @filter.llm_tool(name=MAID_AGENT_TOOL_NAME)
    async def maid_agent(
        self,
        event,
        prompt: str = "",
        subagent_type: str = "",
        run_in_background: bool = False,
        resume_agent_id: str = "",
        tasks: list | None = None,
    ) -> str:
        """Create one or more isolated maid agents from explicit task requests."""
        umo = event.unified_msg_origin
        true_user_input = str(event.get_extra(TRUE_USER_INPUT_EXTRA_KEY) or "")
        batch: list[dict] = []
        if tasks:
            if not isinstance(tasks, list) or not 1 <= len(tasks) <= 5:
                return self._json_outcome({"status": "error", "error": "tasks 必须是包含 1 至 5 项的列表。"})
            if resume_agent_id:
                return self._json_outcome({"status": "error", "error": "批量任务不能使用 resume_agent_id。"})
            for index, item in enumerate(tasks):
                if not isinstance(item, dict):
                    return self._json_outcome({"status": "error", "error": f"tasks[{index}] 必须是对象。"})
                task_prompt = item.get("prompt")
                task_agent = item.get("subagent_type")
                task_background = item.get("run_in_background", run_in_background)
                if not isinstance(task_prompt, str) or not task_prompt.strip() or not isinstance(task_agent, str) or not task_agent.strip() or not isinstance(task_background, bool):
                    return self._json_outcome({"status": "error", "error": f"tasks[{index}] 必须提供 prompt、subagent_type 和布尔 run_in_background。"})
                batch.append({"prompt": task_prompt, "subagent_type": task_agent, "run_in_background": task_background})
        if not batch:
            if not isinstance(prompt, str) or not prompt.strip() or not isinstance(subagent_type, str) or not subagent_type.strip() or not isinstance(run_in_background, bool):
                return self._json_outcome({"status": "error", "error": "必须提供 prompt、subagent_type 和布尔 run_in_background。"})
            batch.append({"prompt": prompt, "subagent_type": subagent_type, "run_in_background": run_in_background, "resume_agent_id": resume_agent_id})

        batch_size = len(batch)
        for item in batch:
            resume_agent_id = str(item.get("resume_agent_id") or "").strip()
            if resume_agent_id:
                if not self.store.exists(resume_agent_id):
                    return self._json_outcome({"status": "error", "error": f"agent_id 不存在: {resume_agent_id}"})
                meta = self.store.log(resume_agent_id).load_meta()
                if meta.get("sourceKind") != "chat" or meta.get("umo") != umo:
                    return self._json_outcome({"status": "error", "error": "agent_id 不属于当前聊天会话。"})
        per_umo_cap = self.maid_mode_config.max_active_per_umo
        global_cap = self.maid_mode_config.max_active_global
        if (
            self.registry.running_count_for_umo(umo) + batch_size > per_umo_cap
            or self.registry.running_count() + batch_size > global_cap
        ):
            return self._json_outcome(
                {"status": "error", "error": "批量并发上限不足，整批拒绝。", "mode": "rejected"}
            )
        prepared: list[dict] = []
        for item in batch:
            resume_agent_id = str(item.get("resume_agent_id") or "").strip()
            if resume_agent_id:
                requested_mode = str(
                    self.store.log(resume_agent_id).load_meta().get("executionMode") or "background"
                )
            else:
                requested_mode = "background" if item["run_in_background"] else self.maid_mode_config.dispatch_session_mode
            effective_mode, background_reason = requested_mode, ""
            dispatch_id = uuid.uuid4().hex
            if requested_mode == "foreground" and not self.registry.acquire_foreground_lease(umo, dispatch_id):
                effective_mode, background_reason = "background", "foreground-umo-busy"
            prepared.append({**item, "dispatch_id": dispatch_id, "execution_mode": effective_mode, "background_reason": background_reason})
        raw_results = await asyncio.gather(
            *(
                self._dispatch_chat_task(event, umo, true_user_input, item, skip_capacity_check=True)
                for item in prepared
            ),
            return_exceptions=True,
        )
        results: list[dict] = []
        for item, result in zip(prepared, raw_results):
            if isinstance(result, Exception):
                self.registry.release_foreground_lease(umo, item["dispatch_id"])
                logger.error("[maid] 派发任务失败: %s", result, exc_info=result)
                results.append({"status": "error", "error": str(result) or "派发任务失败。"})
            else:
                results.append(result)
        if len(results) == 1:
            return self._json_outcome(results[0])
        return self._json_outcome({"status": "batch", "results": results})

    async def _dispatch_chat_task(
        self, event, umo: str, true_user_input: str, item: dict, *, skip_capacity_check: bool = False
    ) -> dict:
        agent_name = item["subagent_type"].strip()
        allowed = self.maid_mode_config.allowed_agent_names
        if agent_name not in allowed:
            agent_name = self.maid_mode_config.default_agent_name
        if agent_name not in allowed:
            self.registry.release_foreground_lease(umo, item["dispatch_id"])
            return {
                "status": "error",
                "error": f"subagent_type 不在允许列表: {item['subagent_type'].strip()}（可用 agents: {', '.join(allowed)}）",
            }
        if not skip_capacity_check and not self.registry.capacity_available(umo):
            return {"status": "error", "error": "并发上限已满，稍后再试。"}

        resume_agent_id = str(item.get("resume_agent_id") or "").strip()
        if resume_agent_id:
            if not self.store.exists(resume_agent_id):
                self.registry.release_foreground_lease(umo, item["dispatch_id"])
                return {"status": "error", "error": f"agent_id 不存在: {resume_agent_id}"}
            session_id = resume_agent_id
        else:
            session_id = self._create_chat_agent(
                umo, agent_name, execution_mode=item["execution_mode"], dispatch_id=item["dispatch_id"], background_reason=item["background_reason"],
            )
        driver = self.registry.attach(session_id)
        driver.execution_mode = item["execution_mode"]
        if item["execution_mode"] == "foreground":
            driver.log.update_meta(foregroundLease=item["dispatch_id"])

        prompt = render_dispatch_prompt(
            self.maid_mode_config.dispatch_prompt_template,
            true_user_input=true_user_input,
            request_text=item["prompt"],
            include_raw_user_input=self.maid_mode_config.include_raw_user_input,
        )

        task_id = uuid.uuid4().hex
        async with driver.log.lock:
            driver.log.append("maid/task", {"taskId": task_id, "dispatchId": item["dispatch_id"]})
        driver.log.update_meta(activeTaskId=task_id, activeDispatchId=item["dispatch_id"], notified=False)
        driver.enqueue(
            c.user_message([c.text_block(prompt)]),
            run_context={"source_event": event, "execution_mode": item["execution_mode"], "dispatch_id": item["dispatch_id"], "task_id": task_id},
        )

        if item["execution_mode"] == "background":
            return {"status": "running", "agent_id": session_id, "task_id": task_id, "dispatch_id": item["dispatch_id"], "mode": "background", **({"background_reason": item["background_reason"]} if item["background_reason"] else {})}

        result = await driver.wait_next_turn_result(timeout=None)
        await driver.emit_delivery("main-summary", "skipped")
        return {"status": result.get("status", "completed"), "agent_id": session_id, "task_id": task_id, "dispatch_id": item["dispatch_id"], "mode": "foreground", "result": result.get("result", ""), "error": result.get("error", "")}

    async def _on_turn_terminal(self, driver, result: dict) -> None:
        meta = driver.log.load_meta()
        if not meta.get("notify"):
            return
        if meta.get("notified"):
            return
        driver.log.update_meta(notified=True)
        try:
            await driver.emit_delivery("main-summary", "sending")
            await self._notify_main_agent(driver, result)
            await driver.emit_delivery("main-summary", "sent")
        except Exception as exc:  # noqa: BLE001
            await driver.emit_delivery("main-summary", "failed", str(exc))
            logger.error("[maid] notification 唤醒主 agent 失败: session=%s err=%s", driver.session_id[:8], exc, exc_info=True)

    async def _notify_main_agent(self, driver, result: dict) -> None:
        """Wake the main agent once for one UMO notification snapshot.（移植）"""
        meta = driver.log.load_meta()
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
        summary = f"[管家任务通知]\n- agent_id={driver.session_id} task_id={meta_task_id(driver)} status={status}\n  {body}"
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

    @filter.llm_tool(name=MAID_SEND_MESSAGE_TOOL_NAME)
    async def maid_send_message(self, event, agent_id: str, message: str) -> str:
        """Send a follow-up message to a running maid agent."""
        if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(message, str) or not message.strip():
            return self._json_outcome({"status": "error", "error": "agent_id 和 message 必须是非空字符串。"})
        if not self._is_current_chat_agent(event, agent_id):
            return self._json_outcome({"status": "error", "error": f"agent_id 不属于当前聊天会话: {agent_id}"})
        driver = self.registry.attach(agent_id)
        if not driver.running:
            return self._json_outcome({"status": "error", "error": "agent 当前没有运行中的任务。"})
        ticket = driver.steer(message)
        return self._json_outcome({"status": "sent", "agent_id": agent_id, "ticket": ticket or ""})

    def _is_current_chat_agent(self, event, agent_id: str) -> bool:
        if not self.store.exists(agent_id):
            return False
        meta = self.store.log(agent_id).load_meta()
        return meta.get("sourceKind") == "chat" and meta.get("umo") == event.unified_msg_origin

    @filter.llm_tool(name=MAID_LIST_AGENTS_TOOL_NAME)
    async def maid_list_agents(self, event) -> str:
        """List agents created from the current chat origin."""
        umo = event.unified_msg_origin
        agents: list[dict] = []
        for agent_id in self.store.list_session_ids():
            meta = self.store.log(agent_id).load_meta()
            if meta.get("sourceKind") != "chat" or meta.get("umo") != umo:
                continue
            driver = self.registry.attach(agent_id)
            agents.append({
                "agent_id": agent_id,
                "task_id": meta.get("activeTaskId", ""),
                "subagent_type": meta.get("agentName", ""),
                "status": "running" if driver.running else meta.get("lastStatus", "idle"),
                "mode": meta.get("executionMode", "background"),
                "dispatch_id": meta.get("activeDispatchId", meta.get("dispatchId", "")),
            })
        return self._json_outcome({"agents": agents})

    @filter.llm_tool(name=MAID_TASK_OUTPUT_TOOL_NAME)
    async def maid_task_output(self, event, task_id: str, block: bool = True, timeout_ms: int = 30000) -> str:
        """Read a task result, optionally waiting for its current turn to settle."""
        if not isinstance(task_id, str) or not task_id.strip() or not isinstance(block, bool) or isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 600000:
            return self._json_outcome({"status": "error", "error": "task_id、block 或 timeout_ms 无效。"})
        agent_id = self._find_agent_for_task(task_id)
        if not agent_id or not self._is_current_chat_agent(event, agent_id):
            return self._json_outcome({"status": "error", "error": f"task_id 不存在: {task_id}"})
        driver = self.registry.attach(agent_id)
        if driver.running and block:
            try:
                result = await driver.wait_next_turn_result(timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                return self._json_outcome({"status": "running", "agent_id": agent_id, "task_id": task_id, "query_status": "timeout"})
            return self._json_outcome({"agent_id": agent_id, "task_id": task_id, **result})
        meta = driver.log.load_meta()
        return self._json_outcome({"agent_id": agent_id, "task_id": task_id, "status": "running" if driver.running else meta.get("lastStatus", "completed"), "result": meta.get("lastResult", ""), "error": meta.get("lastError", "")})

    @filter.llm_tool(name=MAID_TASK_STOP_TOOL_NAME)
    async def maid_task_stop(self, event, task_id: str) -> str:
        """Request cancellation of a task by its task ID."""
        if not isinstance(task_id, str) or not task_id.strip():
            return self._json_outcome({"status": "error", "error": "task_id 必须是非空字符串。"})
        agent_id = self._find_agent_for_task(task_id)
        if not agent_id or not self._is_current_chat_agent(event, agent_id):
            return self._json_outcome({"status": "error", "error": f"task_id 不存在: {task_id}"})
        driver = self.registry.attach(agent_id)
        if not driver.running:
            return self._json_outcome({"status": "completed", "agent_id": agent_id, "task_id": task_id})
        driver.request_stop()
        return self._json_outcome({"status": "stopping", "agent_id": agent_id, "task_id": task_id})


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
