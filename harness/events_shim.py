"""女仆子代理运行时的合成事件。

女仆不借用主流程的真实 event：真实 event 归 pipeline 所有，
``PipelineScheduler.execute`` 在 finally 里清理它的临时文件并注销它，而女仆
此时往往还在跑。这里改为持有一份派发时抽取的身份快照，发送走
``Context.send_message``，聊天来源的女仆因此能真正开口说话。
"""

from __future__ import annotations

import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent as CoreAstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.platform_metadata import PlatformMetadata

from ..constants import DASHBOARD_UMO


def identity_from_event(event: Any) -> dict:
    """从真实聊天 event 抽一份可离线存活的身份快照。"""
    platform_meta = getattr(event, "platform_meta", None)
    message_obj = getattr(event, "message_obj", None)
    return {
        "senderId": str(event.get_sender_id() or ""),
        "senderName": str(event.get_sender_name() or ""),
        "selfId": str(event.get_self_id() or ""),
        "groupId": str(event.get_group_id() or ""),
        "platformName": str(getattr(platform_meta, "name", "") or ""),
        "platformDescription": str(getattr(platform_meta, "description", "") or ""),
        "platformId": str(getattr(platform_meta, "id", "") or ""),
        "supportStreamingMessage": bool(getattr(platform_meta, "support_streaming_message", False)),
        "supportProactiveMessage": bool(getattr(platform_meta, "support_proactive_message", False)),
        "role": str(getattr(event, "role", "member") or "member"),
        "messageStr": str(getattr(message_obj, "message_str", "") or ""),
    }


async def image_paths_from_event(event: Any) -> list[str]:
    """把真实 event 携带的图片落成本地路径，供派发时复制进会话附件区。

    必须在派发时就取：这些是 AstrBot 的临时文件，主 pipeline 结束时会被删除，
    而女仆此时通常还没开始跑。
    """
    paths: list[str] = []
    components = getattr(getattr(event, "message_obj", None), "message", None) or []
    for index, component in enumerate(components):
        if not isinstance(component, Image):
            continue
        try:
            path = await component.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maid] 快照图片失败: index=%s err=%s", index, exc)
            continue
        if path:
            paths.append(str(path))
    return paths


class MaidAgentMessage(AstrBotMessage):
    def __init__(self, *, text: str, session: MessageSession, identity: dict) -> None:
        super().__init__()
        # 必须给真实的 message_type：留空会被基类回退成 FRIEND_MESSAGE，
        # 群里的女仆就会以为自己在私聊。
        self.type = session.message_type
        self.self_id = identity.get("selfId") or "maid"
        self.session_id = session.session_id
        self.message_id = f"maid_{uuid.uuid4().hex}"
        group_id = identity.get("groupId") or ""
        if group_id or session.message_type.value == "GroupMessage":
            self.group = Group(group_id=group_id or session.session_id)
        self.sender = MessageMember(
            user_id=identity.get("senderId") or "maid",
            nickname=identity.get("senderName") or "Maid",
        )
        self.message = []
        self.message_str = text
        self.raw_message = {"source": "maid-agent", "text": text}
        self.image_urls = []


class MaidAgentEvent(CoreAstrMessageEvent):
    """女仆 run 的 AstrMessageEvent 兼容对象。

    身份来自派发时的快照；``send`` 按 UMO 走平台适配器真实投递，控制台来源
    （无真实平台的 ``DASHBOARD_UMO``）静默丢弃。
    """

    def __init__(
        self,
        *,
        context: Any,
        unified_msg_origin: str,
        identity: dict | None = None,
        message_text: str = "",
    ) -> None:
        identity = identity or {}
        session = MessageSession.from_str(unified_msg_origin)
        message_obj = MaidAgentMessage(text=message_text, session=session, identity=identity)
        platform_meta = PlatformMetadata(
            name=identity.get("platformName") or "maid-agent",
            description=identity.get("platformDescription") or "AstrBot Maid Agent",
            id=identity.get("platformId") or session.platform_id,
            support_streaming_message=bool(identity.get("supportStreamingMessage", False)),
            support_proactive_message=bool(identity.get("supportProactiveMessage", False)),
        )
        super().__init__(
            message_str=message_text,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session.session_id,
        )
        self.role = identity.get("role") or "admin"
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.call_llm = False
        self.plugins_name = None
        self._maid_context = context
        self._maid_umo = unified_msg_origin

    @property
    def deliverable(self) -> bool:
        return self._maid_context is not None and self._maid_umo != DASHBOARD_UMO

    async def send(self, message: MessageChain) -> None:
        # 控制台会话没有可投递的平台，直接丢弃——也别让基类记一笔从没发生过的
        # 平台发送指标。
        if not self.deliverable:
            return
        await super().send(message)
        await self._maid_context.send_message(self._maid_umo, message)
