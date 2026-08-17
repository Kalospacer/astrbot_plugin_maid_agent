"""磁盘 session store + 概要索引。

sessions/<sid>/ 由 SessionLog 负责；这里管：
- 会话发现与概要（updatedAt/blank/parentSession/origin/agentPreset/projections 基线）
- 附件（prompt 图片的持久化：attachments/<sid>/<attachmentId>.<ext>）
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import time
import uuid
from pathlib import Path

from ..constants import normalize_umo
from .contracts import now_ms
from .event_log import SessionLog
from .history import derive_surface, visible_events
from .projections import ProjectionRegistry

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{6,64}$")

IMAGE_MEDIA_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def new_session_id() -> str:
    return uuid.uuid4().hex


class SessionStore:
    def __init__(self, root: Path, projections: ProjectionRegistry | None = None):
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.attachments_dir = self.root / "attachments"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.projections = projections or ProjectionRegistry()
        self._logs: dict[str, SessionLog] = {}

    # ------------------------------------------------------------ 日志句柄

    def log(self, session_id: str) -> SessionLog:
        log = self._logs.get(session_id)
        if log is None:
            log = SessionLog(self.sessions_dir, session_id)
            self._logs[session_id] = log
        return log

    def exists(self, session_id: str) -> bool:
        if not SESSION_ID_RE.match(session_id or ""):
            return False
        return self.log(session_id).exists()

    def list_session_ids(self) -> list[str]:
        out = []
        if self.sessions_dir.exists():
            for child in self.sessions_dir.iterdir():
                if child.is_dir() and SESSION_ID_RE.match(child.name):
                    if (child / "header.json").exists():
                        out.append(child.name)
        return sorted(out)

    # ------------------------------------------------------------ 建会话

    def create_session(
        self,
        *,
        session_id: str | None = None,
        parent_session: str | None = None,
        origin: str | None = None,
        delegation_depth: int | None = None,
        agent_preset: str | None = None,
        seed_events: list[dict] | None = None,
        meta: dict | None = None,
    ) -> SessionLog:
        sid = session_id or new_session_id()
        if not SESSION_ID_RE.match(sid):
            raise ValueError(f"非法 sessionId: {sid}")
        if self.exists(sid):
            raise FileExistsError(sid)
        log = self.log(sid)
        header: dict = {"createdAt": now_ms()}
        if parent_session is not None:
            header["parentSession"] = parent_session
        if seed_events:
            header["seedLength"] = len(seed_events)
        if origin is not None:
            header["origin"] = origin
        if delegation_depth is not None:
            header["delegationDepth"] = delegation_depth
        if agent_preset is not None:
            header["agentPreset"] = agent_preset
        log.create(header)
        if seed_events:
            for event in seed_events:
                log.append(
                    event["type"],
                    event.get("data", {}),
                    ignorable=bool(event.get("ignorable")),
                    surface_op=event.get("surfaceOp", "append"),
                    source_event_seqs=event.get("sourceEventSeqs"),
                    time_ms=event.get("time"),
                )
            log.append("session/end-seed", {})
        merged_meta = {
            "createdAt": header["createdAt"],
            "updatedAt": header["createdAt"],
            "blank": True,
        }
        if meta:
            merged_meta.update(meta)
        log.save_meta(merged_meta)
        return log

    # ------------------------------------------------------------ 概要

    def summary(self, session_id: str, *, running: bool = False, with_projections: bool = True) -> dict:
        log = self.log(session_id)
        header = log.load_header() or {}
        meta = log.load_meta()
        events = log.read_events()
        surface = derive_surface(events)
        has_turn = any(e.get("type") == "turn/start" for e in visible_events(events))

        updated_at = int(meta.get("updatedAt") or header.get("createdAt") or 0)
        last_prompt_at = 0
        for event in reversed(surface):
            src = event.get("data", {}).get("source") or {}
            if event.get("type") == "user/message" and src.get("kind") == "user":
                last_prompt_at = int(event.get("time", 0))
                break
        if last_prompt_at:
            updated_at = max(updated_at, last_prompt_at)

        item: dict = {
            "sessionId": session_id,
            "updatedAt": updated_at,
            "running": running,
            "blank": not has_turn,
        }
        if meta.get("umo"):
            item["umo"] = normalize_umo(meta["umo"])
        if header.get("parentSession"):
            item["parentSessionId"] = header["parentSession"]
        if header.get("origin"):
            item["origin"] = header["origin"]
        if header.get("agentPreset"):
            item["agentPreset"] = header["agentPreset"]
        if with_projections:
            item["projections"] = self.projections.compute(session_id, events)
        return item

    def touch(self, session_id: str) -> None:
        log = self.log(session_id)
        meta = log.load_meta()
        meta["updatedAt"] = now_ms()
        log.save_meta(meta)

    # ------------------------------------------------------------ 附件

    def save_attachment(self, session_id: str, media_type: str, data_b64: str, name: str | None = None) -> dict:
        """持久化 base64 图片，返回 ImageAttachmentRef。"""
        ext = IMAGE_MEDIA_EXT.get(media_type)
        if ext is None:
            raise ValueError(f"不支持的图片类型: {media_type}")
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("图片 base64 解码失败。") from exc
        if not raw:
            raise ValueError("图片内容为空。")
        attachment_id = uuid.uuid4().hex
        target_dir = self.attachments_dir / session_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{attachment_id}{ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
        ref: dict = {
            "attachmentId": attachment_id,
            "mediaType": media_type,
            "byteLength": len(raw),
        }
        if name:
            ref["name"] = name
        return ref

    def load_attachment(self, session_id: str, attachment_id: str) -> tuple[dict, bytes]:
        if not ATTACHMENT_ID_RE.match(attachment_id or ""):
            raise ValueError("非法附件 id。")
        target_dir = self.attachments_dir / session_id
        if not target_dir.is_dir():
            raise FileNotFoundError(attachment_id)
        for child in target_dir.iterdir():
            if not child.is_file():
                continue
            stem = child.stem
            if stem != attachment_id:
                continue
            media_type = next(
                (mt for mt, ext in IMAGE_MEDIA_EXT.items() if ext == child.suffix),
                "application/octet-stream",
            )
            with open(child, "rb") as fh:
                raw = fh.read()
            ref = {
                "attachmentId": attachment_id,
                "mediaType": media_type,
                "byteLength": len(raw),
            }
            return ref, raw
        raise FileNotFoundError(attachment_id)

    def attachment_paths_for_prompt(self, session_id: str, refs: list[dict]) -> list[str]:
        """把 user/message 里的 image 块解析成本地文件路径（供 runner 摄取）。"""
        paths: list[str] = []
        for ref in refs:
            attachment_id = str(ref.get("attachmentId") or "")
            try:
                _, raw = self.load_attachment(session_id, attachment_id)
            except (FileNotFoundError, ValueError):
                continue
            ext = IMAGE_MEDIA_EXT.get(str(ref.get("mediaType")), ".png")
            target_dir = self.attachments_dir / session_id
            path = target_dir / f"{attachment_id}{ext}"
            if path.exists():
                paths.append(str(path))
        return paths

    # ------------------------------------------------------------ 维护

    def delete_session(self, session_id: str) -> None:
        import shutil

        log = self.log(session_id)
        log.invalidate_cache()
        target = self.sessions_dir / session_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        attach_dir = self.attachments_dir / session_id
        if attach_dir.is_dir():
            shutil.rmtree(attach_dir, ignore_errors=True)
        self._logs.pop(session_id, None)

    def retention_prune(self, retention_days: int) -> list[str]:
        """清理超过保留期且未运行的会话（由宿主周期调用，运行表由调用方注入）。"""
        cutoff = time.time() - max(1, retention_days) * 86400
        removed: list[str] = []
        for sid in self.list_session_ids():
            log = self.log(sid)
            meta = log.load_meta()
            updated = float(meta.get("updatedAt") or 0) or 0
            if not updated:
                continue
            if updated / 1000 < cutoff:
                self.delete_session(sid)
                removed.append(sid)
        return removed
