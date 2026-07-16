"""Dashboard admin identity and Console image upload regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_maid_agent import main as main_module
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.constants import PLUGIN_DATA_DIR_NAME
from astrbot_plugin_maid_agent.main import MaidAgent, _DashboardMaidEvent
from astrbot_plugin_maid_agent.runtime_orchestrator import DispatchOutcome

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"console-image"


class _Upload:
    def __init__(
        self,
        data: bytes,
        *,
        filename: str = "image.png",
        content_type: str = "image/png",
        content_length: int | None = None,
    ) -> None:
        self.data = data
        self.filename = filename
        self.content_type = content_type
        self.content_length = len(data) if content_length is None else content_length
        self.closed = False

    async def read(self, _size: int = -1) -> bytes:
        return self.data

    async def close(self) -> None:
        self.closed = True


class _DispatchRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def dispatch_single(self, *, event, request, runner_payload=None):
        self.calls.append(
            {"event": event, "request": request, "runner_payload": runner_payload}
        )
        return DispatchOutcome(
            agent_id="a" * 32,
            task_id="1" * 32,
            agent_name=request.agent_name or "butler",
            status="starting",
            mode="background",
        )


def _patch_upload_root(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(main_module, "get_astrbot_temp_path", lambda: str(tmp_path))
    return tmp_path / PLUGIN_DATA_DIR_NAME / "console_uploads"


def _make_console_plugin(body: dict) -> tuple[MaidAgent, _DispatchRecorder]:
    plugin = object.__new__(MaidAgent)
    plugin.maid_mode_config = MaidModeConfig(
        default_agent_name="butler",
        allowed_agent_names=["butler"],
    )
    recorder = _DispatchRecorder()
    plugin.orchestrator = recorder

    async def json_body():
        return body

    async def no_op(*_args, **_kwargs):
        return None

    plugin._console_json_body = json_body
    plugin._console_ensure_task_safe = no_op
    plugin._console_ok = lambda data=None, message=None: {"data": data, "message": message}
    plugin._console_error = lambda message, status_code=400: {
        "error": message,
        "status_code": status_code,
    }
    return plugin, recorder


def test_dashboard_event_is_admin_but_isolated_member_stays_member() -> None:
    dashboard = _DashboardMaidEvent(
        unified_msg_origin="platform:FriendMessage:user",
        sender_id="dashboard",
        message_text="hello",
    )
    assert dashboard.role == "admin"
    assert dashboard.is_admin()

    dashboard.role = "member"
    child = object.__new__(MaidAgent)._isolate_child_event(dashboard)
    assert child.role == "member"
    assert not child.is_admin()


def test_console_image_upload_uses_safe_random_temp_path(monkeypatch, tmp_path: Path) -> None:
    upload_dir = _patch_upload_root(monkeypatch, tmp_path)
    upload = _Upload(PNG_BYTES, filename="../../danger.png")

    payload = asyncio.run(MaidAgent._save_console_image_upload(upload))

    saved = Path(payload["path"])
    assert saved.parent == upload_dir.resolve()
    assert saved.name != "danger.png"
    assert saved.suffix == ".png"
    assert saved.read_bytes() == PNG_BYTES
    assert payload["name"] == "danger.png"
    assert payload["mime_type"] == "image/png"


@pytest.mark.parametrize(
    ("upload", "message"),
    [
        (_Upload(b"", content_type="image/png"), "为空"),
        (_Upload(b"not-an-image", content_type="image/png"), "不是受支持"),
        (_Upload(PNG_BYTES, content_type="image/jpeg"), "不一致"),
        (_Upload(PNG_BYTES, content_type="text/plain"), "仅支持"),
        (
            _Upload(
                PNG_BYTES,
                content_type="image/png",
                content_length=10 * 1024 * 1024 + 1,
            ),
            "10 MB",
        ),
    ],
)
def test_console_image_upload_rejects_invalid_files(
    monkeypatch,
    tmp_path: Path,
    upload: _Upload,
    message: str,
) -> None:
    _patch_upload_root(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=message):
        asyncio.run(MaidAgent._save_console_image_upload(upload))


def test_console_image_paths_cannot_escape_upload_directory(monkeypatch, tmp_path: Path) -> None:
    upload_dir = _patch_upload_root(monkeypatch, tmp_path)
    upload_dir.mkdir(parents=True)
    inside = upload_dir / "safe.png"
    inside.write_bytes(PNG_BYTES)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG_BYTES)

    assert MaidAgent._normalize_console_image_paths([str(inside)]) == [str(inside.resolve())]
    with pytest.raises(ValueError, match="安全临时文件"):
        MaidAgent._normalize_console_image_paths([str(outside)])


def test_console_dispatch_keeps_admin_and_forwards_images(monkeypatch, tmp_path: Path) -> None:
    upload_dir = _patch_upload_root(monkeypatch, tmp_path)
    upload_dir.mkdir(parents=True)
    image_path = upload_dir / "safe.png"
    image_path.write_bytes(PNG_BYTES)
    plugin, recorder = _make_console_plugin(
        {
            "unified_msg_origin": "platform:FriendMessage:user",
            "agent_name": "butler",
            "request_text": "inspect this",
            "run_in_background": True,
            "image_urls_raw": [str(image_path)],
        }
    )

    response = asyncio.run(plugin.console_dispatch())

    assert response["data"]["outcome"]["task_id"] == "1" * 32
    call = recorder.calls[0]
    assert call["event"].role == "admin"
    assert call["event"].get_sender_id() == "dashboard"
    assert call["runner_payload"]["image_urls_raw"] == [str(image_path.resolve())]


def test_console_rerun_keeps_dashboard_admin_identity() -> None:
    plugin, recorder = _make_console_plugin({"task_id": "old-task"})
    run = SimpleNamespace(
        unified_msg_origin="platform:FriendMessage:user",
        sender_id="original-owner",
        request_text="try again",
        agent_name="butler",
    )

    async def find_run(_task_id):
        return SimpleNamespace(), run

    plugin.runtime_store = SimpleNamespace(find_run=find_run)

    response = asyncio.run(plugin.console_rerun())

    assert response["data"]["outcome"]["task_id"] == "1" * 32
    call = recorder.calls[0]
    assert call["event"].role == "admin"
    assert call["event"].get_sender_id() == "original-owner"


def test_console_agent_runs_rejects_invalid_agent_id() -> None:
    plugin, _ = _make_console_plugin({})

    async def load_agent(_agent_id):
        raise ValueError("非法 agent_id")

    plugin.runtime_store = SimpleNamespace(load_agent=load_agent)

    response = asyncio.run(plugin.console_agent_runs("not-an-agent-id"))

    assert response["status_code"] == 400
    assert "非法 agent_id" in response["error"]


def test_console_resume_keeps_admin_and_forwards_images(monkeypatch, tmp_path: Path) -> None:
    upload_dir = _patch_upload_root(monkeypatch, tmp_path)
    upload_dir.mkdir(parents=True)
    image_path = upload_dir / "safe.png"
    image_path.write_bytes(PNG_BYTES)
    plugin, recorder = _make_console_plugin(
        {
            "agent_id": "a" * 32,
            "unified_msg_origin": "platform:FriendMessage:user",
            "request_text": "continue with this",
            "image_urls_raw": [str(image_path)],
        }
    )
    plugin.runtime_store = SimpleNamespace(
        load_agent=lambda _agent_id: None,
    )

    async def load_agent(_agent_id):
        return SimpleNamespace(
            unified_msg_origin="platform:FriendMessage:user",
            sender_id="original-owner",
        )

    plugin.runtime_store.load_agent = load_agent

    response = asyncio.run(plugin.console_resume())

    assert response["data"]["outcome"]["task_id"] == "1" * 32
    call = recorder.calls[0]
    assert call["event"].role == "admin"
    assert call["event"].get_sender_id() == "original-owner"
    assert call["request"].resume_agent_id == "a" * 32
    assert call["runner_payload"]["image_urls_raw"] == [str(image_path.resolve())]
