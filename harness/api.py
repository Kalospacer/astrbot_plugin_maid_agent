"""RPC 方法实现与分发（对应 apiproxy 的 api-proxy.ts + fetch/handler.ts）。

方法表 = RpcMethodMap 的子集：session.* / agentPreset.* / settings.* / host.*。
响应经 rpc.server_response 包装（HTTP 200 业务错误）；SSE 流由 main.py 的
web api 处理器消费 StreamHub。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ._log import logger

from ..constants import DASHBOARD_UMO
from . import contracts as c
from . import tools_view
from .history import derive_surface, history_page, visible_events
from ..config import ConfigValidationError
from .rpc import RpcError, bad_request, session_not_found
from .store import new_session_id

SETTINGS_NS = "maid"

SETTINGS_KEYS = {
    "allowed_agent_names",
    "default_agent_name",
    "hide_native_tools",
    "hide_transfer_tools",
    "include_raw_user_input",
    "log_raw_llm_io",
    "dispatch_prompt_template",
    "dispatch_session_mode",
    "memory_agent_names",
    "max_active_per_umo",
    "max_active_global",
    "retention_days",
    "max_turn_seconds",
}


class ApiProxy:
    def __init__(
        self,
        *,
        store,
        registry,
        config_holder,
    ):
        self.store = store
        self.registry = registry
        self.config_holder = config_holder
        self.methods = {
            "session.list": self.session_list,
            "session.search": self.session_search,
            "session.create": self.session_create,
            "session.history": self.session_history,
            "session.models": self.session_models,
            "session.selectModel": self.session_select_model,
            "session.rename": self.session_rename,
            "session.fork": self.session_fork,
            "session.delete": self.session_delete,
            "session.prompt": self.session_prompt,
            "session.attachment": self.session_attachment,
            "session.updateQueue": self.session_update_queue,
            "session.cancel": self.session_cancel,
            "agentPreset.list": self.preset_list,
            "agentPreset.select": self.preset_select,
            "settings.describe": self.settings_describe,
            "settings.update": self.settings_update,
            "settings.replace": self.settings_replace,
            "settings.mutate": self.settings_mutate,
            "host.describe": self.host_describe,
        }
        self._settings_revision = 1

    async def dispatch(self, method: str, payload: dict, rpc_id: str = ""):
        handler = self.methods.get(method)
        if handler is None:
            raise bad_request(f"未知方法: {method}")
        if rpc_id and method == "session.prompt":
            payload = {**payload, "_rpcId": rpc_id}
        return await handler(payload)


    def _require_session(self, session_id: str):
        if not isinstance(session_id, str) or not self.store.exists(session_id):
            raise session_not_found(session_id or "")
        return self.store.log(session_id)

    async def session_list(self, payload: dict) -> dict:
        items = []
        for sid in self.store.list_session_ids():
            try:
                driver = self.registry.drivers.get(sid)
                items.append(self.store.summary(sid, running=bool(driver and driver.running)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[maid] session 概要失败: %s err=%s", sid[:8], exc)
        items.sort(key=lambda item: item.get("updatedAt", 0), reverse=True)
        return {"items": items}

    async def session_search(self, payload: dict) -> dict:
        query = str(payload.get("query") or "").strip().lower()
        if not query:
            return {"items": [], "hasMore": False}
        umo_filter = str(payload.get("umo") or "").strip()
        items = []
        for sid in self.store.list_session_ids():
            log = self.store.log(sid)
            if umo_filter and str(log.load_meta().get("umo") or "") != umo_filter:
                continue
            best = ""
            for event in derive_surface(log.read_events()):
                data = event.get("data", {})
                blocks = data.get("content") or data.get("message", {}).get("content") or []
                text = "".join(
                    block.get("text", "")
                    for block in blocks
                    if block.get("type") == "text"
                )
                if query in text.lower():
                    idx = text.lower().find(query)
                    start = max(0, idx - 30)
                    best = text[start : idx + len(query) + 50]
                    break
            if best:
                items.append({"sessionId": sid, "snippet": best})
            if len(items) >= 20:
                break
        return {"items": items, "hasMore": False}

    async def session_create(self, payload: dict) -> dict:
        preset = str(payload.get("agentPreset") or "").strip()
        if not preset:
            raise RpcError("agent-preset-required", "控制台任务必须显式选择 Agent。", {})
        if preset not in getattr(self.registry.config, "allowed_agent_names", ()):
            raise RpcError("agent-preset-not-allowed", f"Agent 不在允许列表: {preset}", {"agentPreset": preset})
        umo = DASHBOARD_UMO
        sender_id = "dashboard"
        session_id = new_session_id()
        log = self.store.create_session(
            session_id=session_id,
            agent_preset=preset,
            meta={
                "umo": umo,
                "senderId": sender_id,
                "agentName": preset,
                "agentId": session_id,
                "sourceKind": "dashboard",
                "notify": False,
            },
        )
        driver = self.registry.attach(str(log.session_id))
        driver.umo, driver.agent_name, driver.sender_id = umo, preset, sender_id
        self.registry.publish_host_frame(
            c.frame_host_session_added(
                log.session_id,
                True,
                agentPreset=preset,
                sourceKind="dashboard",
            )
        )
        return {"sessionId": log.session_id, "agentPreset": preset}

    async def session_history(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        log = self._require_session(session_id)
        events = log.read_events()
        before_seq = payload.get("beforeSeq")
        before_seq = int(before_seq) if isinstance(before_seq, int) else None
        max_messages = payload.get("maxMessages")
        max_messages = int(max_messages) if isinstance(max_messages, int) and max_messages > 0 else 30
        page = history_page(events, before_seq=before_seq, max_messages=max_messages)

        from . import tools_view as tv

        entries = []
        call_args: dict[str, tuple[str, str]] = {}
        for event in page["events"]:
            entry: dict = {"event": event}
            etype = event.get("type")
            data = event.get("data", {})
            if etype == "tool/call":
                call_args[data.get("callId")] = (data.get("name", ""), data.get("arguments", ""))
                view = tv.present_call(data.get("name", ""), data.get("arguments", ""))
                if view:
                    entry["view"] = c.tool_event_view_call(view)
            elif etype == "tool/result":
                block = (data.get("message", {}).get("content") or [{}])[0] if data.get("message", {}).get("content") else {}
                result_content = block.get("content", []) if isinstance(block, dict) else []
                text = "".join(
                    part.get("text", "")
                    for part in result_content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
                name, arguments = call_args.get(data.get("message", {}).get("source", {}).get("callId", ""), ("", None))
                view = tv.present_result(data.get("error", {}).get("name") or name, text, arguments)
                if view:
                    entry["view"] = c.tool_event_view_result(view)
            entries.append(entry)

        result: dict = {"events": entries, "hasMore": page["has_more"]}
        if before_seq is None:
            result["projections"] = self.store.projections.compute(session_id, events)
        return result

    async def session_models(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        log = self._require_session(session_id)
        meta = log.load_meta()
        override = str(meta.get("providerId") or "").strip()

        handoff_provider = ""
        driver = self.registry.drivers.get(session_id)
        agent_name = (driver.agent_name if driver else "") or str(meta.get("agentName") or "")
        try:
            handoff, _ = self.registry.resolve_handoff(
                agent_name
            )
            handoff_provider = str(getattr(handoff, "provider_id", None) or "")
        except Exception:  # noqa: BLE001
            pass

        umo = str(meta.get("umo") or DASHBOARD_UMO)
        effective = (
            override
            or handoff_provider
            or await self.registry.context.get_current_chat_provider_id(umo)
        )
        providers = []
        current_model = ""
        for provider in self.registry.context.get_all_providers():
            pmeta = provider.meta()
            providers.append({"id": pmeta.id, "model": pmeta.model or "", "type": pmeta.type})
            if pmeta.id == effective:
                current_model = pmeta.model or ""
        return {
            "current": {"provider": effective, "model": current_model, "override": bool(override)},
            "providers": providers,
        }

    async def session_select_model(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        log = self._require_session(session_id)
        provider_id = str(payload.get("provider") or "").strip()
        if provider_id:
            chat_provider_ids = {
                p.meta().id for p in self.registry.context.get_all_providers()
            }
            if provider_id not in chat_provider_ids:
                raise RpcError(
                    "provider-not-found",
                    f"未找到聊天 provider: {provider_id}",
                    {"sessionId": session_id},
                )
        log.update_meta(providerId=provider_id)
        driver = self.registry.drivers.get(session_id)
        if driver is not None:
            driver.provider_id = provider_id
        model = ""
        if provider_id:
            provider = self.registry.context.get_provider_by_id(provider_id)
            model = str(provider.meta().model or "") if provider is not None else ""
        return {"selected": {"provider": provider_id, "model": model}}

    async def session_rename(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        log = self._require_session(session_id)
        title = " ".join(str(payload.get("title") or "").split())
        if not title:
            raise RpcError("title-invalid", "标题规范化后为空。", {"sessionId": session_id})
        driver = self.registry.driver(session_id)
        if driver is None:
            event = log.append("session/title", {"title": title, "source": {"kind": "user"}})
            self.registry.publish_event_frame(session_id, event)
            self.registry.push_projection_changes(session_id)
        else:
            driver.append_title(title, "user")
        log.update_meta(pinnedTitle=title)
        return {"title": title, "seq": self.store.log(session_id).last_seq}

    async def session_delete(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        self._require_session(session_id)
        driver = self.registry.drivers.get(session_id)
        if driver is not None and driver.running:
            raise RpcError(
                "session-running",
                "运行中的会话不能删除，请先停止任务。",
                {"sessionId": session_id},
            )
        self.store.delete_session(session_id)
        self.registry.drivers.pop(session_id, None)
        self.registry.publish_host_frame(c.frame_host_session_removed(session_id))
        return {"deleted": True}

    async def session_fork(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        log = self._require_session(session_id)
        events = visible_events(log.read_events())
        at_seq = payload.get("atSeq")
        at_seq = int(at_seq) if isinstance(at_seq, int) else None

        if at_seq is not None:
            boundary = None
            open_turn = False
            turn_open = False
            for event in events:
                if event.get("type") == "turn/start":
                    turn_open = True
                if event["seq"] >= at_seq and event.get("type") == "turn/end":
                    boundary = event["seq"]
                    turn_open = False
                    break
            if boundary is None and turn_open:
                raise RpcError("fork-unavailable", "目标 turn 仍在进行中。", {"sessionId": session_id})
            if boundary is not None:
                events = [e for e in events if e["seq"] <= boundary]
        else:
            last_end = None
            for event in events:
                if event.get("type") == "turn/end":
                    last_end = event["seq"]
            if last_end is not None:
                events = [e for e in events if e["seq"] <= last_end]

        header = log.load_header() or {}
        meta = log.load_meta()
        child = self.store.create_session(
            parent_session=session_id,
            agent_preset=header.get("agentPreset"),
            seed_events=events,
            meta={
                "umo": meta.get("umo", ""),
                "senderId": meta.get("senderId", ""),
                "agentName": meta.get("agentName", ""),
            },
        )
        driver = self.registry.attach(child.session_id)
        driver.umo = str(meta.get("umo") or "")
        driver.agent_name = str(meta.get("agentName") or "")
        driver.sender_id = str(meta.get("senderId") or "")
        self.registry.publish_host_frame(
            c.frame_host_session_added(child.session_id, True, parentSessionId=session_id, agentPreset=header.get("agentPreset"))
        )
        return {"sessionId": child.session_id}

    async def session_prompt(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        self._require_session(session_id)
        driver = self.registry.attach(session_id)
        mode = str(payload.get("mode") or "queue")
        content = payload.get("content") or []
        if not isinstance(content, list) or not content:
            raise bad_request("content 不能为空。")

        blocks: list[dict] = []
        for part in content:
            ptype = part.get("type")
            if ptype == "text":
                text = str(part.get("text") or "")
                if text:
                    blocks.append(c.text_block(text))
            elif ptype == "image":
                try:
                    ref = self.store.save_attachment(
                        session_id,
                        str(part.get("mediaType") or ""),
                        str(part.get("data") or ""),
                        part.get("name"),
                    )
                except ValueError as exc:
                    raise RpcError("attachment-error", str(exc), {"reason": str(exc)}) from exc
                blocks.append(c.image_block(ref))

        if not blocks:
            raise bad_request("content 没有可发送的块。")

        if mode == "steer" and driver.running:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            driver.steer(text)
            return {"accepted": True}

        if not self.registry.capacity_available(driver.umo):
            raise RpcError("agent-busy", "并发上限已满，稍后重试。", {"reason": "capacity"})
        rpc_id = str(payload.get("_rpcId") or "")
        message = (
            c.user_rpc_message(blocks, rpc_id, payload.get("clientTimeZone"))
            if rpc_id
            else c.user_message(blocks)
        )
        driver.enqueue(message)
        return {"accepted": True}

    async def session_attachment(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        attachment_id = str(payload.get("attachmentId") or "")
        log = self._require_session(session_id)
        referenced = any(
            block.get("type") == "image"
            and (block.get("attachment") or {}).get("attachmentId") == attachment_id
            for event in log.read_events()
            if event.get("type") == "user/message"
            for block in event.get("data", {}).get("content", [])
        )
        if not referenced:
            raise RpcError("attachment-error", "会话未引用该附件。", {"reason": "not-referenced"})
        try:
            ref, raw = self.store.load_attachment(session_id, attachment_id)
        except FileNotFoundError as exc:
            raise RpcError("attachment-error", "附件不存在。", {"reason": "missing"}) from exc
        return {"attachment": ref, "data": base64.b64encode(raw).decode("ascii")}

    async def session_update_queue(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        self._require_session(session_id)
        driver = self.registry.attach(session_id)
        driver.update_queue_item(str(payload.get("itemId") or ""), payload.get("action") or {})
        return {"accepted": True}

    async def session_cancel(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        self._require_session(session_id)
        driver = self.registry.driver(session_id)
        if driver is not None:
            driver.request_stop()
        return {"accepted": True}


    def _preset_entries(self) -> list[dict]:
        entries: dict[str, dict] = {}
        try:
            orchestrator = getattr(self.registry.context, "subagent_orchestrator", None)
            handoffs = getattr(orchestrator, "handoffs", None) or []
            for handoff in handoffs:
                agent = getattr(handoff, "agent", None)
                if agent is None:
                    continue
                name = str(getattr(agent, "name", "") or "")
                if not name:
                    continue
                entries[name] = {
                    "id": name,
                    "trust": "system",
                    "name": name,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 读取 subagent 列表失败: %s", exc)
        allowed = set(getattr(self.registry.config, "allowed_agent_names", ()))
        return sorted((entry for name, entry in entries.items() if name in allowed), key=lambda e: e["id"])

    async def preset_list(self, payload: dict) -> dict:
        return {"presets": self._preset_entries(), "authorable": False, "hasDocument": False}

    async def preset_select(self, payload: dict) -> dict:
        session_id = str(payload.get("sessionId") or "")
        preset = str(payload.get("agentPreset") or "")
        log = self._require_session(session_id)
        if any(e.get("type") == "turn/start" for e in log.read_events()):
            raise RpcError(
                "agent-preset-locked",
                "会话已开始对话，不能更换 agent 组合。",
                {"sessionId": session_id, "agentPreset": preset},
            )
        if not preset or preset not in {entry["id"] for entry in self._preset_entries()}:
            raise RpcError(
                "agent-preset-not-found",
                f"未知的 agent 组合: {preset}",
                {"agentPreset": preset, "available": [e["id"] for e in self._preset_entries()]},
            )
        log.update_meta(agentName=preset)
        header = log.load_header() or {}
        header["agentPreset"] = preset
        import os

        header_path = log.header_path
        tmp = header_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(header, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, header_path)
        driver = self.registry.driver(session_id)
        if driver is not None:
            driver.agent_name = preset
        return {"agentPreset": preset}


    def _settings_view(self) -> dict:
        value = self.config_holder.get_config()
        return {
            "ns": SETTINGS_NS,
            "schema": self.config_holder.settings_schema(),
            "value": value,
            "applies": "live",
            "secrets": [],
            "revision": self._settings_revision,
        }

    async def settings_describe(self, payload: dict) -> dict:
        return {
            "writable": True,
            "hasDocument": False,
            "namespaces": [self._settings_view()],
        }

    def _apply_settings_patch(self, payload: dict) -> dict:
        ns = str(payload.get("ns") or "")
        if ns != SETTINGS_NS:
            raise RpcError("settings-rejected", f"未知配置命名空间: {ns}", {"ns": ns})
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            raise RpcError("settings-rejected", "patch 必须是对象。", {"ns": ns})
        unknown = [key for key in patch if key not in SETTINGS_KEYS]
        if unknown:
            raise RpcError("settings-rejected", f"未知配置键: {','.join(unknown)}", {"ns": ns})
        try:
            self.config_holder.save_config(patch)
        except ConfigValidationError as exc:
            raise RpcError("settings-rejected", "配置验证失败。", {"errors": exc.errors}) from exc
        self._settings_revision += 1
        return self._settings_view()

    async def settings_update(self, payload: dict) -> dict:
        return self._apply_settings_patch(payload)

    async def settings_replace(self, payload: dict) -> dict:
        ns = str(payload.get("ns") or "")
        if ns != SETTINGS_NS:
            raise RpcError("settings-rejected", f"未知配置命名空间: {ns}", {"ns": ns})
        section = payload.get("section")
        if not isinstance(section, dict):
            raise RpcError("settings-rejected", "section 必须是对象。", {"ns": ns})
        return self._apply_settings_patch({"ns": ns, "patch": section})

    async def settings_mutate(self, payload: dict) -> dict:
        ns = str(payload.get("ns") or "")
        patch: dict = {}
        for op in payload.get("ops") or []:
            if not isinstance(op, dict) or op.get("op") != "set":
                continue
            path = op.get("path") or []
            if not path:
                continue
            key = str(path[0])
            if key in SETTINGS_KEYS:
                patch[key] = op.get("value")
        return self._apply_settings_patch({"ns": ns, "patch": patch})


    async def host_describe(self, payload: dict) -> dict:
        return {
            "version": self.config_holder.version(),
            "cwd": str(self.store.root),
            "attachedSessions": self.registry.running_count(),
            "canOpenPath": False,
        }
