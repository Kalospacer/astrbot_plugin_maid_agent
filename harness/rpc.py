"""四象限 RPC 信封与错误码表（对应 apiproxy/src/api/rpc.ts + rpc.schema.ts）。

桥注意事项：dashboard 父窗口把插件 HTTP 响应解成
``response.data?.data ?? response.data``，且 ``status === "error"`` 视为失败。
ServerResponse 信封没有 status/data 字段，因此可以原样穿透给前端 client。
"""

from __future__ import annotations

import uuid


class RpcError(Exception):
    """业务错误。code 必须来自闭合错误码表。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


# 错误码表（照抄 RpcErrorDetailsMap 的键；details 形状按需填充）
RPC_ERROR_CODES = {
    "bad-request",
    "cancelled",
    "session-not-found",
    "model-unavailable",
    "session-conflict",
    "invalid-time-zone",
    "workspace-attach-failed",
    "workspace-not-found",
    "workspace-invalid-path",
    "workspace-name-conflict",
    "workspace-move-invalid",
    "directory-unreadable",
    "directory-exists",
    "directory-create-failed",
    "directory-picker-unavailable",
    "agent-preset-read-only",
    "agent-preset-locked",
    "agent-preset-conflict",
    "agent-preset-not-found",
    "agent-preset-invalid",
    "agent-busy",
    "attachment-error",
    "queue-item-not-found",
    "steer-unavailable",
    "command-error",
    "unknown-command",
    "settings-rejected",
    "settings-not-exposed",
    "settings-conflict",
    "credential-rejected",
    "model-discovery-failed",
    "title-invalid",
    "fork-unavailable",
    "subagent-parent-unavailable",
    "subagent-not-found",
    "subagent-catalog-diagnostic",
    "subagent-not-resumable",
    "subagent-unauthorized",
    "subagent-delivery-unavailable",
    "internal",
}


def session_not_found(session_id: str) -> RpcError:
    return RpcError("session-not-found", f"会话不存在: {session_id}", {"sessionId": session_id})


def agent_busy(reason: str) -> RpcError:
    return RpcError("agent-busy", reason, {"reason": reason})


def bad_request(message: str, issues: list | None = None) -> RpcError:
    return RpcError("bad-request", message, {"issues": issues or []})


def internal_error(message: str) -> RpcError:
    return RpcError("internal", message, {})


# ---------------------------------------------------------------- 信封


def parse_client_request(body: dict) -> tuple[str, str, dict]:
    """校验并拆出 (rpcId, method, payload)。形状不符抛 bad-request。"""
    if not isinstance(body, dict):
        raise bad_request("请求体必须是 JSON 对象。")
    if body.get("type") != "client-request":
        raise bad_request("请求体缺少 type: client-request 信封。")
    rpc_id = body.get("rpcId")
    method = body.get("method")
    if not isinstance(rpc_id, str) or not rpc_id:
        raise bad_request("请求缺少 rpcId。")
    if not isinstance(method, str) or not method:
        raise bad_request("请求缺少 method。")
    payload = body.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise bad_request("payload 必须是 JSON 对象。")
    return rpc_id, method, payload


def server_response(rpc_id: str, value) -> dict:
    return {
        "type": "server-response",
        "rpcId": rpc_id,
        "result": {"ok": True, "value": value},
    }


def server_response_error(rpc_id: str, error: RpcError) -> dict:
    return {
        "type": "server-response",
        "rpcId": rpc_id,
        "result": {"ok": False, "error": error.to_dict()},
    }


def server_request(rpc_id: str, method: str, payload) -> dict:
    return {"type": "server-request", "rpcId": rpc_id, "method": method, "payload": payload}


def client_response_receipt(accepted: bool, reason: str | None = None) -> dict:
    if accepted:
        return {"accepted": True}
    return {"accepted": False, "reason": reason or "bad-response"}


def new_rpc_id() -> str:
    return str(uuid.uuid4())
