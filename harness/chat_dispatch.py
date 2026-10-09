"""模型侧五工具的任务路由；session 只负责上下文，不决定任务身份。"""

from __future__ import annotations

import uuid
from copy import deepcopy

from ..config import render_dispatch_prompt
from ..constants import DISPATCHED_NEXT_STEP, TRUE_USER_INPUT_EXTRA_KEY
from ..maid_dispatcher import ensure_default_subagent
from . import contracts as c
from .context_state import ContextState, freeze_messages
from .events_shim import identity_from_event, image_paths_from_event
from .tasks import ACTIVE_STATUSES

MAIN_CONTEXT_KEY = "maid_main_run_context"
MAIN_REQUEST_KEY = "maid_main_provider_request"


class ChatRuntime:
    def __init__(self, plugin):
        self.plugin = plugin
        self.registry = plugin.registry
        self.tasks = self.registry.tasks
        self.chats = self.registry.chats

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
        """主对话被 new/reset：作废旧代次、取消未执行内容并换一份状态。"""
        async with self.chats.lock(umo):
            previous = self.chats.get(umo)
            if previous is not None:
                self.invalidate(previous)
            cid = await self.plugin.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                self.chats.reset(umo, cid)

    async def snapshot(self, event, state: dict) -> list[dict]:
        run_context = event.get_extra(MAIN_CONTEXT_KEY)
        if run_context is not None:
            return freeze_messages(run_context.messages)
        req = event.get_extra(MAIN_REQUEST_KEY)
        if req is not None:
            records = freeze_messages(req.contexts or [])
            current = await req.assemble_context()
            return [*records, *freeze_messages([current])]
        conv = await self.plugin.context.conversation_manager.get_conversation(event.unified_msg_origin, state["conversationId"])
        import json

        return freeze_messages(json.loads(conv.history or "[]")) if conv is not None else []

    def invalidate(self, state: dict) -> None:
        """成功 new/reset 后取消旧代次运行和未消费要求。"""
        for task in self.chats.task_list(state):
            self.tasks.cancel_followups(task["taskId"])
            driver = self.registry.drivers.get(task["sessionId"])
            if driver is not None and driver.busy:
                driver.request_stop()
        self.registry.invalid_epochs.add(state["epoch"])

    def allowed(self, execution: dict, umo: str) -> bool:
        if execution.get("delivery_cancelled", False):
            return False
        return self.registry.epoch_active(umo, execution.get("epoch"))

    def error(self, state: dict, message: str) -> dict:
        return {"status": "error", "error": message, "tasks": [self.tasks.card(task) for task in self.chats.task_list(state)]}

    async def dispatch(self, event, *, prompt: str = "", task_id: str = "", force_new: bool = False, tasks: list | None = None) -> dict:
        umo = event.unified_msg_origin
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
                if any(not isinstance(item, dict) or set(item) != {"prompt"} or not isinstance(item["prompt"], str) or not item["prompt"].strip() for item in tasks):
                    return self.error(state, "每项任务只接受非空 prompt。")
                requests = [item["prompt"].strip() for item in tasks]
            else:
                if not isinstance(prompt, str) or not prompt.strip():
                    return self.error(state, "必须提供非空 prompt，或使用 tasks 批量派发。")
                requests = [prompt.strip()]
            active = self.chats.active(state)
            target = None
            if task_id:
                try:
                    target = self.chats.target(state, task_id)
                except ValueError as exc:
                    return self.error(state, str(exc))
                if target["status"] in ACTIVE_STATUSES:
                    return self.error(state, "目标任务正在执行。补充要求请使用 maid_send_message；并行新建请设置 force_new=true。")
                branch = target["branchId"]
            elif batch or force_new:
                branch = None
            else:
                if active:
                    return self.error(state, "已有任务正在执行。补充要求请使用 maid_send_message；并行新建请设置 force_new=true。")
                branch = self.chats.automatic_branch(state)
                if state["branches"] and branch is None:
                    return self.error(state, "有多份工作记录且尚未选择默认会话。继续某项请指定 task_id；独立新建请设置 force_new=true。")
            existing_sid = state["branches"].get(branch) if branch is not None else None
            existing_driver = self.registry.drivers.get(existing_sid) if existing_sid else None
            if existing_driver is not None and existing_driver.busy:
                return self.error(state, "目标工作上下文正被运行任务使用，不排队。补充运行任务请使用 maid_send_message，独立新建请设置 force_new=true。")
            await ensure_default_subagent(self.plugin.context, self.plugin.maid_mode_config)
            agent_name = self.plugin.maid_mode_config.default_agent_name
            self.registry.resolve_handoff(agent_name)
            main_context = await self.snapshot(event, state)
            identity = identity_from_event(event)
            images = await image_paths_from_event(event)
            if event.get_extra("agent_stop_requested"):
                return self.error(state, "主会话已停止或重置，取消本次派发。")
            # 所有 await 已完成后再统一预留，启动中的任务也占容量。
            cfg = self.plugin.maid_mode_config
            if not self.registry.capacity_available(umo, len(requests)):
                return self.error(state, "并发上限不足，整批拒绝。")
            if batch or (force_new and active):
                state["defaultBranch"] = None
            results = []
            for request in requests:
                selected_branch = uuid.uuid4().hex if batch or force_new or branch is None else branch
                current_task = target if target is not None else self.tasks.create(scope=self.chats.key(umo), epoch=state["epoch"], branch=selected_branch, request=request)
                sid = state["branches"].get(selected_branch)
                if sid is None:
                    sid = self.plugin._create_chat_agent(umo, agent_name, dispatch_id=uuid.uuid4().hex, identity=identity)
                    self.plugin.store.log(sid).update_meta(epoch=state["epoch"], conversationId=state["conversationId"], branchId=selected_branch, contextGeneration=state["contextGeneration"])
                    state["branches"][selected_branch] = sid
                driver = self.registry.attach(sid)
                driver.agent_name = agent_name
                driver.log.update_meta(agentName=agent_name, identity=identity)
                rendered = render_dispatch_prompt(cfg.dispatch_prompt_template, true_user_input=str(event.get_extra(TRUE_USER_INPUT_EXTRA_KEY) or ""), request_text=request, include_raw_user_input=cfg.include_raw_user_input)
                content = [c.text_block(rendered)]
                for path in images:
                    ref = self.plugin.store.save_attachment_from_path(sid, path)
                    if ref is not None:
                        content.append(c.image_block(ref))
                execution = self.tasks.start_round(current_task["taskId"], sid, request)
                run_context = {
                    "task_id": current_task["taskId"],
                    "round_id": execution["roundId"],
                    "epoch": state["epoch"],
                    "conversation_id": state["conversationId"],
                    "main_context": deepcopy(main_context),
                }
                driver.log.update_meta(activeTaskId=current_task["taskId"])
                driver.enqueue(c.user_message(content), run_context=run_context)
                if not active and not batch:
                    state["defaultBranch"] = selected_branch
                results.append({**self.tasks.card(current_task), "round_id": execution["roundId"], "status": "running"})
            self.chats.begin(state)
            outcome = {"status": "batch", "tasks": results} if batch else results[0]
            outcome["next"] = DISPATCHED_NEXT_STEP
            return outcome

    async def send(self, event, task_id: str, message: str) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            if not isinstance(message, str) or not message.strip():
                return self.error(state, "message 必须是非空字符串。")
            try:
                if not task_id:
                    active = self.chats.active(state)
                    if len(active) != 1:
                        return self.error(state, "存在多个目标或没有运行任务，请明确指定 task_id。")
                    task_id = active[0]["taskId"]
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            driver = self.registry.drivers.get(task["sessionId"])
            if task["status"] not in ACTIVE_STATUSES or driver is None or not driver.busy:
                return self.error(state, "目标任务没有运行。完成后继续请使用 maid_agent，不会自动转换操作。")
            main = await self.snapshot(event, state)
            followup = self.tasks.add_followup(task_id, message.strip(), main)
            text = ContextState(driver.log.dir).followup_text(main, message.strip())
            # 启动中没有 follow_up handler 时只可靠保存，不能 enqueue 成另一项任务。
            if driver.running and driver._steer_fn is not None:
                driver.steer(text, followup_id=followup["id"], main_context=main)
            return {**self.tasks.card(task), "status": "accepted", "followup_id": followup["id"], "consumption": "pending"}

    async def list(self, event) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            records = self.chats.task_list(state)
            default = state["defaultBranch"]
            return {"tasks": [{**self.tasks.card(task), "is_default": task["branchId"] == default} for task in records]}

    async def output(self, event, task_id: str) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            try:
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            outcome = self.tasks.card(task)
            outcome["followups"] = [{"id": item["id"], "status": item["status"]} for item in task["followups"]]
            driver = self.registry.drivers.get(task["sessionId"])
            if task["status"] in ACTIVE_STATUSES and driver is not None:
                outcome.update(self.plugin._agent_progress(driver))
            else:
                outcome["rounds"] = [{"round_id": item["roundId"], "status": item["status"], "result": item.get("result", ""), "error": item.get("error", "")} for item in task["rounds"]]
                for execution in task["rounds"]:
                    if self.tasks.claim_delivery(task_id, execution["roundId"]):
                        self.tasks.delivery(task_id, execution["roundId"], "read")
            return outcome

    async def stop(self, event, task_id: str) -> dict:
        async with self.chats.lock(event.unified_msg_origin):
            state = await self.state(event)
            try:
                task = self.chats.target(state, task_id)
            except ValueError as exc:
                return self.error(state, str(exc))
            self.tasks.cancel_followups(task_id)
            driver = self.registry.drivers.get(task["sessionId"])
            if task["status"] in ACTIVE_STATUSES and driver is not None:
                driver.request_stop()
                return {**self.tasks.card(task), "status": "stopping"}
            return self.tasks.card(task)
