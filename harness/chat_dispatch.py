"""大小姐侧工具的任务路由：派发、追加输入、列出、读结果和停止；以及管家的 maid_read。

session 只负责上下文，不决定任务身份。有人发消息触发的派发是显式请求；
CronMessageEvent（自我唤醒）触发的派发是推断候选，要填评估表单。
"""

from __future__ import annotations

import json
import uuid

from astrbot.core.cron.events import CronMessageEvent

from ..config import render_dispatch_prompt
from ..constants import DISPATCHED_NEXT_STEP
from ..maid_dispatcher import ensure_default_subagent
from .admission import FORM_FIELDS, Admission, AdmissionError, assess, parse_form
from .events_shim import identity_from_event, image_paths_from_event
from .tasks import add_input, new_assignment, running_assignment

REQUEST_PREVIEW_CHARS = 1500
TRIGGER_PREVIEW_CHARS = 600
INPUT_PREVIEW_CHARS = 1500
READ_DEFAULT_LIMIT = 4000
READ_MAX_LIMIT = 8000


def _truncate(text: str, limit: int, resource: str) -> str:
    """超长文字截断，并注明可用 maid_read 读全文。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…\n（已截断，全文共 {len(text)} 字，可用 maid_read 读取 resource={resource}）"


def _render_history(history: list) -> str:
    """把主对话历史渲染成逐行文本：序号、角色、正文；媒体写成占位符。"""
    media_labels = {"image_url": "[图片]", "audio_url": "[音频]", "file": "[文件]"}
    lines = []
    for index, message in enumerate(history):
        if not isinstance(message, dict) or message.get("role") in ("system", "_checkpoint"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "") if part.get("type") == "text" else media_labels.get(part.get("type"), "")
                for part in content
                if isinstance(part, dict)
            ]
            text = "".join(parts)
        else:
            text = str(content or "")

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {} if isinstance(call, dict) else {}
            text += f"\n[调用 {function.get('name', '')} {function.get('arguments', '')}]"
        lines.append(f"[{index}] {message.get('role')}: {text.strip()}")
    return "\n".join(lines)


class ChatRuntime:
    def __init__(self, plugin):
        self.plugin = plugin
        self.registry = plugin.registry
        self.tasks = self.registry.tasks
        self.chats = self.registry.chats
        self.decisions = self.registry.decisions

    async def state(self, event) -> dict:
        umo = event.unified_msg_origin
        cid = await self.plugin.context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            cid = await self.plugin.context.conversation_manager.new_conversation(umo)
        previous = self.chats.get(umo)
        if previous is not None and previous["conversationId"] != cid:
            self.invalidate(previous)
        state = self.chats.ensure(umo, cid)
        self.chats.touch_tool(state, self.plugin.maid_mode_config.session_idle_timeout_hours)
        for sid in state["branches"].values():
            self.plugin.store.touch(sid)
        return state

    async def restart(self, umo: str) -> None:
        """主对话被 new/reset：作废旧代次、停掉正在跑的管家并换一份状态。"""
        async with self.chats.lock(umo):
            previous = self.chats.get(umo)
            if previous is not None:
                self.invalidate(previous)
            cid = await self.plugin.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                self.chats.reset(umo, cid)

    def invalidate(self, state: dict) -> None:
        """旧代次的任务不再启动、送达或唤醒；正在跑的这一轮被取消。"""
        for task in self.chats.task_list(state):
            driver = self.registry.drivers.get(task["sessionId"])
            if driver is not None and driver.busy:
                driver.request_stop()
        self.registry.invalid_epochs.add(state["epoch"])

    def error(self, state: dict, message: str) -> dict:
        return {"status": "error", "error": message, "tasks": [self.tasks.card(task) for task in self.chats.task_list(state)]}

    def _admit(self, raw: dict) -> tuple[Admission, object]:
        form = parse_form(raw)
        return assess(form, self.plugin.maid_mode_config.inferred_rules), form

    def _dispatch_text(self, item: dict, mandate: dict | None) -> str:
        """派发给管家的第一条消息：截断后的触发消息与请求，推断派发再附上命中的规则。"""
        cfg = self.plugin.maid_mode_config
        text = render_dispatch_prompt(
            cfg.dispatch_prompt_template,
            true_user_input=_truncate(item.get("trigger", ""), TRIGGER_PREVIEW_CHARS, f"trigger/{item['id']}"),
            request_text=_truncate(item["full"], REQUEST_PREVIEW_CHARS, f"input/{item['id']}"),
            include_raw_user_input=cfg.include_raw_user_input,
        )
        if not mandate:
            return text
        # 对应 superagent src/executor/dsh.ts：命中规则的说明优先于默认的答复与沟通方式。
        rules = {rule.id: rule for rule in cfg.inferred_rules}
        lines = [f"\n- {rules[rule_id].name} ({rule_id}): {rules[rule_id].instruction}" for rule_id in mandate["rules"] if rule_id in rules]
        scope = f"自主接下，依据规则 {', '.join(mandate['rules'])}。范围：{mandate['scope']}。" if mandate["rules"] else f"自主接下。范围：{mandate['scope']}。"
        precedence = f"\n命中的规则；它们的做法与沟通要求优先于本任务默认的答复和消息方式：{''.join(lines)}" if lines else ""
        return f"{text}\n\n{scope}{precedence}"

    async def dispatch(self, event, *, prompt: str = "", task_id: str = "", force_new: bool = False, tasks: list | None = None, form: dict | None = None) -> dict:
        umo = event.unified_msg_origin
        inferred = isinstance(event, CronMessageEvent)
        cfg = self.plugin.maid_mode_config
        async with self.chats.lock(umo):
            state = await self.state(event)
            if not isinstance(force_new, bool):
                return self.error(state, "force_new 必须是布尔值。")
            if task_id and force_new:
                return self.error(state, "task_id 和 force_new 不能同时使用。")
            batch = tasks is not None
            if batch:
                if task_id or force_new or prompt:
                    return self.error(state, "tasks 不能与单任务 prompt、task_id 或 force_new 混用。")
                if not isinstance(tasks, list) or not 2 <= len(tasks) <= 5:
                    return self.error(state, "tasks 必须包含 2 至 5 项独立任务。")
                allowed_keys = {"prompt", *FORM_FIELDS}
                if any(not isinstance(item, dict) or not set(item) <= allowed_keys or not isinstance(item.get("prompt"), str) or not item["prompt"].strip() for item in tasks):
                    return self.error(state, "每项任务需要非空 prompt，只能附带评估表单字段。")
                requests = [(item["prompt"].strip(), item) for item in tasks]
            else:
                if not isinstance(prompt, str) or not prompt.strip():
                    return self.error(state, "必须提供非空 prompt，或使用 tasks 批量派发。")
                requests = [(prompt.strip(), form or {})]

            admissions: list[tuple[Admission, object] | None] = [None] * len(requests)
            if inferred:
                if not cfg.inferred_dispatch:
                    return self.error(state, "自我唤醒时不允许派发（inferred_dispatch 已关闭）。")
                try:
                    admissions = [self._admit(raw) for _, raw in requests]
                except AdmissionError as exc:
                    return self.error(state, str(exc))

            working = self.chats.working(state)
            target = None
            if task_id:
                try:
                    target = self.chats.target(state, task_id)
                except ValueError as exc:
                    return self.error(state, str(exc))
                if not target.get("ended"):
                    return self.error(state, "任务尚未结束。追加要求请使用 maid_send_message；并行新建请设置 force_new=true。")
                branch = target["branchId"]
            elif batch or force_new:
                branch = None
            else:
                if working:
                    return self.error(state, "已有任务正在执行。追加要求请使用 maid_send_message；并行新建请设置 force_new=true。")
                branch = self.chats.automatic_branch(state)
                if state["branches"] and branch is None:
                    return self.error(state, "有多份工作记录且尚未选择默认会话。继续某项请指定 task_id；独立新建请设置 force_new=true。")

            await ensure_default_subagent(self.plugin.context, cfg)
            agent_name = cfg.default_agent_name
            try:
                self.registry.resolve_handoff(agent_name)
            except ValueError as exc:
                return self.error(state, str(exc))
            identity = identity_from_event(event)
            images = await image_paths_from_event(event)
            if event.get_extra("agent_stop_requested"):
                return self.error(state, "主会话已停止或重置，取消本次派发。")

            # 以下不再 await：任务、会话绑定、判断记录与 launch 在同一个事务里提交。
            trigger = str(event.message_str or "")
            strength = "inferred" if inferred else "explicit"
            if batch or (force_new and working):
                state["defaultBranch"] = None
            results = []
            with self.registry.db.transaction():
                for (request, _), admitted in zip(requests, admissions):
                    if admitted is not None and not admitted[0].accepted:
                        admission, parsed = admitted
                        self.decisions.record(
                            kind="ignore", strength=strength, reason=admission.reason, task_id=target["taskId"] if target else "",
                            trigger=trigger, form=parsed, matched=admission.matched,
                        )
                        results.append({"status": "ignored", "reason": admission.reason})
                        continue

                    selected_branch = uuid.uuid4().hex if batch or force_new or branch is None else branch
                    sid = state["branches"].get(selected_branch)
                    if sid is None:
                        sid = self.plugin._create_chat_agent(umo, agent_name, dispatch_id=uuid.uuid4().hex, identity=identity)
                        self.plugin.store.log(sid).update_meta(epoch=state["epoch"], conversationId=state["conversationId"], branchId=selected_branch, contextGeneration=state["contextGeneration"])
                        state["branches"][selected_branch] = sid
                    driver = self.registry.attach(sid)
                    driver.agent_name = agent_name
                    driver.log.update_meta(agentName=agent_name, identity=identity)
                    refs = [ref for ref in (self.plugin.store.save_attachment_from_path(sid, path) for path in images) if ref is not None]

                    mandate = None
                    if admitted is not None:
                        admission, parsed = admitted
                        mandate = {"rules": list(admission.matched), "scope": parsed.scope}
                    if target is not None:
                        task = target
                        task.update(ended=None, attention=None, delivery=None)
                        add_input(task, "", human=not inferred, kind="resumed")
                    else:
                        task = self.tasks.create(
                            scope=self.chats.key(umo), epoch=state["epoch"], umo=umo, conversation_id=state["conversationId"],
                            branch=selected_branch, request=request, strength=strength,
                        )
                    item = add_input(task, "", human=not inferred, full=request, trigger=trigger, images=refs)
                    item["text"] = self._dispatch_text(item, mandate)
                    assignment = new_assignment(task, sid)
                    self.tasks.save(task)
                    self.registry.queue_launch(task, assignment, human=not inferred and target is not None)

                    if inferred:
                        self.decisions.record(
                            kind="reopen" if target is not None else "accept", strength=strength, reason="",
                            task_id=task["taskId"], trigger=trigger, form=parsed, matched=admission.matched,
                        )
                    elif target is None:
                        self.decisions.record(kind="accept", strength=strength, reason="explicitRouted", task_id=task["taskId"], trigger=trigger)
                    if not working and not batch:
                        state["defaultBranch"] = selected_branch
                    results.append({**self.tasks.card(task), "status": "dispatched"})
                dispatched = any(item["status"] == "dispatched" for item in results)
                if dispatched:
                    self.chats.begin(state)
                else:
                    self.chats.save(state)

            outcome = {"status": "batch", "tasks": results} if batch else results[0]
            if dispatched:
                outcome["next"] = DISPATCHED_NEXT_STEP
            return outcome

    async def send(self, event, task_id: str, message: str, form: dict | None = None) -> dict:
        inferred = isinstance(event, CronMessageEvent)
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            if not isinstance(message, str) or not message.strip():
                return self.error(state, "message 必须是非空字符串。")
            if inferred and not self.plugin.maid_mode_config.inferred_dispatch:
                return self.error(state, "自我唤醒时不允许派发（inferred_dispatch 已关闭）。")
            try:
                if not task_id:
                    candidates = self.chats.open(state)
                    if len(candidates) != 1:
                        return self.error(state, "存在多个未结束的任务或没有未结束的任务，请明确指定 task_id。")
                    task_id = candidates[0]["taskId"]
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            if task.get("ended"):
                return self.error(state, "任务已结束。继续请使用 maid_agent(task_id)，不会自动转换操作。")

            if inferred:
                try:
                    admission, parsed = self._admit(form or {})
                except AdmissionError as exc:
                    return self.error(state, str(exc))
                if not admission.accepted:
                    self.decisions.record(
                        kind="ignore", strength="inferred", reason=admission.reason, task_id=task_id,
                        trigger=str(event.message_str or ""), form=parsed, matched=admission.matched,
                    )
                    return {**self.tasks.card(task), "status": "ignored", "reason": admission.reason}

            text = message.strip()
            with self.registry.db.transaction():
                item = add_input(task, "", human=not inferred, full=text)
                item["text"] = "【补充要求】\n" + _truncate(text, INPUT_PREVIEW_CHARS, f"input/{item['id']}")
                self.tasks.save(task)
                # 排队中的执行启动时会一并带上；进行中的执行由 delivery 送达。
                if running_assignment(task) is not None:
                    self.registry.queue_delivery(task)
            return {**self.tasks.card(task), "status": "accepted", "input_id": item["id"]}

    async def list(self, event) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            default = state["defaultBranch"]
            return {"tasks": [{**self.tasks.card(task), "is_default": task["branchId"] == default} for task in self.chats.task_list(state)]}

    async def output(self, event, task_id: str) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            try:
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            outcome = self.tasks.card(task)
            outcome["assignments"] = [
                {"assignment_id": item["id"], "state": item["state"], "report": item.get("report"), "ended_reason": item.get("endedReason", "")}
                for item in task["assignments"]
            ]
            driver = self.registry.drivers.get(task["sessionId"])
            if driver is not None and driver.running and driver.current_execution.get("task_id") == task_id:
                outcome.update(self.plugin._agent_progress(driver))
            return outcome

    async def stop(self, event, task_id: str) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            try:
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            strength = "inferred" if isinstance(event, CronMessageEvent) else "explicit"
            return {**self.registry.stop_task(task, strength=strength), "status": "stopped"}

    async def read(self, driver, resource: str, offset: int, limit: int) -> dict:
        """管家按需分页读取本任务的资源：主对话（读取时的最新内容）、请求原文与触发消息原文。"""
        task_id = driver.current_execution.get("task_id")
        if not task_id:
            return {"status": "error", "error": "此工具只能在派发的任务里使用。"}
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return {"status": "error", "error": "offset 必须是不小于 0 的整数。"}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_MAX_LIMIT:
            return {"status": "error", "error": f"limit 必须是 1 至 {READ_MAX_LIMIT} 的整数。"}

        task = self.tasks.get(task_id)
        if resource == "conversation":
            conv = await self.plugin.context.conversation_manager.get_conversation(task["umo"], task["conversationId"])
            text = _render_history(json.loads(conv.history or "[]")) if conv is not None else ""
        else:
            kind, _, input_id = resource.partition("/")
            item = next((entry for entry in task["inputs"] if entry["id"] == input_id), None)
            if kind not in ("input", "trigger") or item is None:
                return {"status": "error", "error": "未知资源。可读：conversation、input/<id>、trigger/<id>。"}
            text = item.get("full", item["text"]) if kind == "input" else item.get("trigger", "")

        page = text[offset : offset + limit]
        end = offset + len(page)
        return {"resource": resource, "text": page, "next_offset": end if end < len(text) else None, "total_chars": len(text)}
