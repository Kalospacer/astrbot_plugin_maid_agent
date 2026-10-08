"""实际运行消息的持久化，以及主对话增量和工作记录的组织。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from copy import deepcopy
from pathlib import Path

from .tasks import FORMAT_VERSION, write_json

BACKGROUND_HEADER = (
    "【主代理对话记录：背景资料】\n"
    "以下是主对话中的记录，并非本子代理已经执行的步骤。"
    "仅作为背景；本次任务以末尾派发要求为准，新的修正优先于旧记录。\n"
)


def freeze_messages(messages: list) -> list[dict]:
    """复制运行消息，不改动主 runner；本地媒体转为稳定 data URI。"""
    copied = []
    for message in messages:
        item = deepcopy(message if isinstance(message, dict) else message.model_dump())
        if item.get("role") == "_checkpoint":
            continue
        copied.append(item)
    _freeze_media(copied)
    return copied


def _freeze_media(value) -> None:
    if isinstance(value, list):
        for item in value:
            _freeze_media(item)
    elif isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and not url.startswith(("http://", "https://", "data:")):
            local = Path(url.removeprefix("file://"))
            if local.is_file():
                media_type = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
                value["url"] = f"data:{media_type};base64," + base64.b64encode(local.read_bytes()).decode()
        for item in value.values():
            _freeze_media(item)


def fingerprint(message: dict) -> str:
    encoded = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def main_block(records: list[dict]) -> dict:
    """背景来源单独标注，多媒体保留为内容块，不能冒充子代理工具执行。"""
    parts = [{"type": "text", "text": BACKGROUND_HEADER}]
    for record in records:
        copy = deepcopy(record)
        media = []
        if isinstance(copy.get("content"), list):
            text_parts = []
            for part in copy["content"]:
                if part.get("type") in ("image_url", "audio_url"):
                    media.append(part)
                else:
                    # 主模型的推理签名不作为另一个模型的有效签名发送。
                    text_parts.append(part)
            copy["content"] = text_parts
        parts.append({"type": "text", "text": json.dumps(copy, ensure_ascii=False) + "\n"})
        parts.extend(media)
    from astrbot.core.agent.message import Message

    return Message.model_validate({"role": "user", "content": parts}).model_dump()


class ContextState:
    def __init__(self, session_dir: Path):
        self.path = session_dir / "context.json"
        # 一轮里每步都会读写这份快照，缓存避免重复读盘；实例之间仍以文件为准。
        self._state: dict | None = None

    def load(self) -> dict:
        if self._state is not None:
            return self._state
        if not self.path.exists():
            self._state = {
                "version": FORMAT_VERSION,
                "messages": [],
                "mainContext": [],
                "backgroundHashes": [],
                "systemPrompt": "",
            }
            return self._state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("version") != FORMAT_VERSION:
            raise ValueError("上下文格式不受支持，不能从旧展示日志恢复。")
        self._state = state
        return state

    def _save(self, state: dict) -> None:
        self._state = state
        write_json(self.path, state)

    @staticmethod
    def _update(state: dict, main_context: list[dict]) -> tuple[list[dict], bool]:
        previous = state["mainContext"]
        if main_context == previous:
            return [], False
        if main_context[:len(previous)] == previous:
            return main_context[len(previous):], False
        return main_context, True

    def prepare(self, main_context: list[dict] | None) -> list[dict]:
        state = self.load()
        if main_context is not None:
            incoming = freeze_messages(main_context)
            delta, rebase = self._update(state, incoming)
            if delta:
                block = main_block(delta)
                if rebase:
                    hashes = set(state["backgroundHashes"])
                    work = [item for item in state["messages"] if fingerprint(item) not in hashes]
                    state["messages"] = [block, *work]
                    state["backgroundHashes"] = [fingerprint(block)]
                else:
                    state["messages"].append(block)
                    state["backgroundHashes"].append(fingerprint(block))
            elif rebase:
                hashes = set(state["backgroundHashes"])
                state["messages"] = [item for item in state["messages"] if fingerprint(item) not in hashes]
                state["backgroundHashes"] = []
            state["mainContext"] = incoming
            self._save(state)
        return deepcopy(state["messages"])

    def followup_text(self, main_context: list[dict], request: str) -> str:
        # 调用方传进来的主背景已经冻结过，这里再复制一遍只是浪费。
        state = self.load()
        delta, rebase = self._update(state, main_context)
        label = "【当前主背景更新：旧背景已压缩或改写】" if rebase else "【主对话新增记录】"
        context_text = label + "\n" + json.dumps(delta, ensure_ascii=False) + "\n" if delta else ""
        return context_text + "【运行中补充要求】\n" + request

    def mark_main_synced(self, main_context: list[dict]) -> None:
        state = self.load()
        state["mainContext"] = freeze_messages(main_context)
        self._save(state)

    def capture(self, messages: list, system_prompt: str) -> None:
        actual = freeze_messages(messages)
        # runner 在头部加入子人格；每轮从配置读取，避免恢复时重复加入。
        if actual and actual[0].get("role") == "system":
            actual = actual[1:]
        state = self.load()
        if state["messages"] == actual and state["systemPrompt"] == system_prompt:
            # 这一步没有新增内容，不必重写整份快照。
            return
        state["messages"] = actual
        state["systemPrompt"] = system_prompt
        hashes = {fingerprint(item) for item in actual}
        state["backgroundHashes"] = [key for key in state["backgroundHashes"] if key in hashes]
        self._save(state)

    def checkpoint(self, turn: int) -> None:
        write_json(self.path.parent / "context_checkpoints" / f"{turn}.json", self.load())

    def fork_state(self, turn: int) -> dict:
        checkpoint = self.path.parent / "context_checkpoints" / f"{turn}.json"
        if not checkpoint.exists():
            raise ValueError("目标轮次没有新格式上下文快照，不能从展示日志恢复。")
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if state.get("version") != FORMAT_VERSION:
            raise ValueError("目标上下文格式不受支持。")
        return state

    def seed(self, state: dict) -> None:
        if state.get("version") != FORMAT_VERSION:
            raise ValueError("上下文格式不受支持。")
        write_json(self.path, deepcopy(state))
