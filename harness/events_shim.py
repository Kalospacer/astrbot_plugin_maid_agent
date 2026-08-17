"""Dashboard 合成事件（移植自旧 main.py::_DashboardMaidEvent）。"""

from __future__ import annotations

import uuid

from astrbot.api.event import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent as CoreAstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.platform_metadata import PlatformMetadata


class DashboardMessage(AstrBotMessage):
    def __init__(self, *, text: str, sender_id: str, session: MessageSession) -> None:
        self.type = None  # 由 platform_meta 决定；占位
        self.self_id = "dashboard"
        self.session_id = session.session_id
        self.message_id = f"dashboard_{uuid.uuid4().hex}"
        self.group_id = session.session_id if session.message_type.value == "GroupMessage" else ""
        self.sender = MessageMember(user_id=sender_id, nickname="Dashboard")
        self.message = []
        self.message_str = text
        self.raw_message = {"source": "dashboard", "text": text}
        self.image_urls = []


class DashboardMaidEvent(CoreAstrMessageEvent):
    """控制台触发 run 所需的最小 AstrMessageEvent 兼容对象。"""

    def __init__(self, *, unified_msg_origin: str, sender_id: str, message_text: str) -> None:
        session = MessageSession.from_str(unified_msg_origin)
        message_obj = DashboardMessage(
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
