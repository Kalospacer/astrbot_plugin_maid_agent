"""大小姐管家模式插件的严格配置契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Any

from .constants import MISTRESS_REQUEST_BLOCK_LABEL, USER_INPUT_BLOCK_LABEL

DEFAULT_ALLOWED_AGENT_NAMES = ("butler",)
DEFAULT_MAX_ACTIVE_PER_UMO = 5
DEFAULT_MAX_ACTIVE_GLOBAL = 20
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_TURN_SECONDS = 1800
DEFAULT_DISPATCH_SESSION_MODE = "background"
DEFAULT_DISPATCH_PROMPT_TEMPLATE = (
    "{user_input_block}{maid_request_block}"
    "你是MuiceMaid，大小姐的管家。你的任务是完成大小姐交给你的请求，"
    "并向大小姐汇报结果。"
)
_DISPATCH_PROMPT_FIELDS = frozenset({"user_input_block", "maid_request_block"})


class ConfigValidationError(ValueError):
    """配置不符合公开契约，errors 使用字段名作为 key。"""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))


@dataclass(slots=True, frozen=True)
class MaidModeConfig:
    allowed_agent_names: tuple[str, ...] = DEFAULT_ALLOWED_AGENT_NAMES
    hide_native_tools: bool = True
    hide_transfer_tools: bool = True
    include_raw_user_input: bool = True
    log_raw_llm_io: bool = False
    dispatch_prompt_template: str = DEFAULT_DISPATCH_PROMPT_TEMPLATE
    dispatch_session_mode: str = DEFAULT_DISPATCH_SESSION_MODE
    memory_agent_names: tuple[str, ...] = ()
    max_active_per_umo: int = DEFAULT_MAX_ACTIVE_PER_UMO
    max_active_global: int = DEFAULT_MAX_ACTIVE_GLOBAL
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_turn_seconds: int = DEFAULT_MAX_TURN_SECONDS


def _safe_int(value: Any, default: int) -> int:
    """AstrBot provider settings use loose values; plugin config never uses this helper."""
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_template(template: Any) -> str:
    if not isinstance(template, str) or not template.strip():
        raise ConfigValidationError({"dispatch_prompt_template": "必须是非空字符串。"})
    try:
        fields = [field for _, field, _, _ in Formatter().parse(template) if field is not None]
    except ValueError as exc:
        raise ConfigValidationError({"dispatch_prompt_template": f"模板格式无效: {exc}"}) from exc
    unknown = sorted(set(fields) - _DISPATCH_PROMPT_FIELDS)
    if unknown:
        raise ConfigValidationError(
            {"dispatch_prompt_template": f"包含未知占位符: {', '.join(unknown)}"}
        )
    return template


def _strict_names(value: Any, field: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigValidationError({field: "必须是字符串列表。"})
    names = tuple(value)
    if not allow_empty and not names:
        raise ConfigValidationError({field: "不能为空。"})
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ConfigValidationError({field: "每项必须是非空字符串。"})
    if len({name.casefold() for name in names}) != len(names):
        raise ConfigValidationError({field: "不能包含重复名称。"})
    return tuple(name.strip() for name in names)


def _strict_bool(cfg: Mapping[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if not isinstance(value, bool):
        raise ConfigValidationError({key: "必须是布尔值。"})
    return value


def _strict_int(cfg: Mapping[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigValidationError({key: f"必须是不小于 {minimum} 的整数。"})
    return value


def load_maid_mode_config(config: Mapping[str, Any] | None = None) -> MaidModeConfig:
    """Validate plugin settings without coercion, repair, aliases, or hidden fallbacks."""
    cfg = dict(config or {})
    allowed = _strict_names(cfg.get("allowed_agent_names", DEFAULT_ALLOWED_AGENT_NAMES), "allowed_agent_names", allow_empty=False)
    memory = _strict_names(cfg.get("memory_agent_names", ()), "memory_agent_names", allow_empty=True)
    unknown_memory_agents = sorted(set(memory) - set(allowed))
    if unknown_memory_agents:
        raise ConfigValidationError(
            {"memory_agent_names": f"必须属于 allowed_agent_names: {', '.join(unknown_memory_agents)}"}
        )
    mode = cfg.get("dispatch_session_mode", DEFAULT_DISPATCH_SESSION_MODE)
    if mode not in {"foreground", "background"}:
        raise ConfigValidationError({"dispatch_session_mode": "必须是 foreground 或 background。"})
    return MaidModeConfig(
        allowed_agent_names=allowed,
        hide_native_tools=_strict_bool(cfg, "hide_native_tools", True),
        hide_transfer_tools=_strict_bool(cfg, "hide_transfer_tools", True),
        include_raw_user_input=_strict_bool(cfg, "include_raw_user_input", True),
        log_raw_llm_io=_strict_bool(cfg, "log_raw_llm_io", False),
        dispatch_prompt_template=_validate_template(cfg.get("dispatch_prompt_template", DEFAULT_DISPATCH_PROMPT_TEMPLATE)),
        dispatch_session_mode=mode,
        memory_agent_names=memory,
        max_active_per_umo=_strict_int(cfg, "max_active_per_umo", DEFAULT_MAX_ACTIVE_PER_UMO, minimum=1),
        max_active_global=_strict_int(cfg, "max_active_global", DEFAULT_MAX_ACTIVE_GLOBAL, minimum=1),
        retention_days=_strict_int(cfg, "retention_days", DEFAULT_RETENTION_DAYS, minimum=1),
        max_turn_seconds=_strict_int(cfg, "max_turn_seconds", DEFAULT_MAX_TURN_SECONDS, minimum=0),
    )


def render_dispatch_prompt(
    template: str,
    *,
    true_user_input: str,
    request_text: str,
    include_raw_user_input: bool,
) -> str:
    """Render an already-validated task prompt; invalid templates are errors, never repaired."""
    validated = _validate_template(template)
    request = request_text.strip()
    if not request:
        raise ValueError("prompt 不能为空。")
    user_input_block = ""
    if include_raw_user_input and true_user_input.strip():
        user_input_block = f"【{USER_INPUT_BLOCK_LABEL}】\n{true_user_input}\n\n"
    maid_request_block = f"【{MISTRESS_REQUEST_BLOCK_LABEL}】\n{request}\n\n"
    return validated.format_map(
        {"user_input_block": user_input_block, "maid_request_block": maid_request_block}
    ).strip()
