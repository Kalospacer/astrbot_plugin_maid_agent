"""
大小姐管家模式插件 - 1.3.0 Child Toolset Adapter & Memory

职责：
- 在插件内复现 AstrBot ``FunctionToolExecutor._build_handoff_toolset`` 的工具选择
  规则，避免继续依赖 Core 私有方法。
- 从 child 工具集中移除 ``call_maid``、``maid_task`` 和所有 ``transfer_to_*``
  handoff 工具，禁止递归调度。
- 对 ``memory_agent_names`` 命中的 agent 自动补齐 AstrBot 原生 Read/Write/Edit
  文件工具（仍走原始权限检查）。
- 实时加载 ``MEMORY.md``，最多内联 200 行且不超过 25000 bytes；超限提示拆分
  topic 文件。不实现 Claude 的 memory snapshot。
- memory 以 ``UMO + agent_name`` 隔离；不随 30 天 transcript retention 删除。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence, Set
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Image
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.image_ref_utils import is_supported_image_ref
from astrbot.core.utils.string_utils import normalize_and_dedupe_strings

if TYPE_CHECKING:
    from astrbot.api.star import Context
    from astrbot.core.agent.handoff import HandoffTool

from .constants import PLUGIN_DATA_DIR_NAME

MEMORY_SUBDIR = "memory"
MEMORY_INDEX_FILENAME = "MEMORY.md"
MEMORY_MAX_LINES = 200
MEMORY_MAX_BYTES = 25_000

# Tool names that must never reach a child worker (control plane / recursion).
_RECURSION_TOOL_NAMES = frozenset({"call_maid", "maid_task"})
_HANDOFF_TOOL_PREFIX = "transfer_to_"

# Builtin file tools auto-added to memory-enabled children.
_FILE_TOOL_CLASS_NAMES = ("FileReadTool", "FileWriteTool", "FileEditTool")

# Kept in contract-test parity with AstrBot 4.26.6
# FunctionToolExecutor._get_runtime_computer_tools. Production code deliberately
# avoids calling that private Core method.
_SANDBOX_TOOL_CLASS_NAMES = (
    "ExecuteShellTool",
    "PythonTool",
    "FileUploadTool",
    "FileDownloadTool",
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GrepTool",
)
_LOCAL_TOOL_CLASS_NAMES = (
    "ExecuteShellTool",
    "LocalPythonTool",
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GrepTool",
)
_CUA_TOOL_CLASS_NAMES = (
    "CuaScreenshotTool",
    "CuaMouseClickTool",
    "CuaKeyboardTypeTool",
)


def _is_handoff_tool(tool: FunctionTool) -> bool:
    return tool.name.startswith(_HANDOFF_TOOL_PREFIX)


def _is_recursion_tool(tool: FunctionTool) -> bool:
    return tool.name in _RECURSION_TOOL_NAMES


def _sanitize_child_toolset(tools: ToolSet | None) -> ToolSet | None:
    """Strip maid control-plane and handoff tools from a child's toolset.

    Returns None if the result is empty (caller treats None as "no tools").
    """
    if tools is None:
        return None
    sanitized = ToolSet()
    for tool in tools.tools:
        if not getattr(tool, "active", True):
            continue
        if _is_recursion_tool(tool) or _is_handoff_tool(tool):
            continue
        sanitized.add_tool(tool)
    return None if sanitized.empty() else sanitized


def _get_builtin_file_tools(context: Context) -> dict[str, FunctionTool]:
    """Resolve the AstrBot builtin file tools (read/write/edit) by class.

    These are the same tools the main agent gets in local computer-use mode, so
    they already carry the platform's permission/audit machinery.
    """
    try:
        from astrbot.core.tools import computer_tools as ct
    except ImportError:  # pragma: no cover - defensive
        logger.warning("[大小姐模式] 无法导入 computer_tools，memory 文件工具将不可用。")
        return {}
    tool_mgr = context.get_llm_tool_manager()
    if tool_mgr is None:
        return {}
    result: dict[str, FunctionTool] = {}
    for class_name in _FILE_TOOL_CLASS_NAMES:
        cls = getattr(ct, class_name, None)
        if cls is None:
            continue
        try:
            tool = tool_mgr.get_builtin_tool(cls)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[大小姐模式] 获取内置文件工具失败: %s err=%s", class_name, exc
            )
            continue
        if tool is not None and getattr(tool, "active", True):
            result[tool.name] = tool
    return result


def build_child_toolset(
    context: Context,
    *,
    handoff: HandoffTool,
    umo: str,
    agent_name: str,
    memory_agent_names: list[str] | None,
) -> ToolSet | None:
    """Build a sanitized child toolset, reproducing AstrBot's handoff selection.

    Replicates ``FunctionToolExecutor._build_handoff_toolset``:
    - ``tools=None`` means "all tools except handoffs", including runtime
      computer-use tools.
    - A list of names resolves registered + runtime tools.
    - Empty list means no tools.

    Then strips recursion/handoff tools and optionally augments with file tools
    for memory-enabled agents.
    """
    provider_settings = _load_provider_settings(context, umo)
    runtime = str(provider_settings.get("computer_use_runtime", "local"))
    tool_mgr = context.get_llm_tool_manager()
    runtime_computer_tools = _get_runtime_computer_tools(runtime, tool_mgr, provider_settings)

    raw_tools = getattr(handoff.agent, "tools", None)
    toolset = ToolSet()

    if raw_tools is None:
        # "all tools" — mirror Core: every active registered tool except handoffs.
        handoff_names = {
            t.name for t in (tool_mgr.func_list if tool_mgr else []) if _is_handoff_tool(t)
        }
        full = tool_mgr.get_full_tool_set() if tool_mgr else ToolSet()
        for registered in full.tools:
            if registered.name in handoff_names:
                continue
            if getattr(registered, "active", True):
                toolset.add_tool(registered)
        for rt_tool in runtime_computer_tools.values():
            toolset.add_tool(rt_tool)
    elif raw_tools:
        for item in raw_tools:
            if isinstance(item, FunctionTool):
                toolset.add_tool(item)
                continue
            name = str(item).strip()
            if not name:
                continue
            registered = tool_mgr.get_func(name) if tool_mgr else None
            if registered and getattr(registered, "active", True):
                toolset.add_tool(registered)
                continue
            rt_tool = runtime_computer_tools.get(name)
            if rt_tool is not None:
                toolset.add_tool(rt_tool)
    # else: empty list -> no tools.

    sanitized = _sanitize_child_toolset(toolset)

    if _agent_memory_enabled(memory_agent_names, agent_name):
        sanitized = _augment_with_file_tools(sanitized, context)

    return sanitized


async def collect_child_image_urls(event, image_urls_raw: Any) -> list[str]:
    """Plugin-owned equivalent of Core's private handoff image collector."""
    candidates: list[str] = []
    if isinstance(image_urls_raw, str):
        candidates.append(image_urls_raw)
    elif isinstance(image_urls_raw, (Sequence, Set)) and not isinstance(
        image_urls_raw, (str, bytes, bytearray)
    ):
        candidates.extend(item for item in image_urls_raw if isinstance(item, str))
    for index, component in enumerate(getattr(getattr(event, "message_obj", None), "message", []) or []):
        if not isinstance(component, Image):
            continue
        try:
            path = await component.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[大小姐模式] 转换 child 图片失败: index=%s err=%s",
                index,
                exc,
            )
            continue
        if path:
            candidates.append(path)
    normalized = normalize_and_dedupe_strings(candidates)
    return [
        item
        for item in normalized
        if is_supported_image_ref(
            item,
            allow_extensionless_existing_local_file=True,
            extensionless_local_roots=(get_astrbot_temp_path(),),
        )
    ]


def _augment_with_file_tools(
    toolset: ToolSet | None,
    context: Context,
) -> ToolSet | None:
    file_tools = _get_builtin_file_tools(context)
    if not file_tools:
        return toolset
    result = toolset if toolset is not None else ToolSet()
    for tool in file_tools.values():
        result.add_tool(tool)
    return None if result.empty() else result


def _agent_memory_enabled(
    memory_agent_names: list[str] | None,
    agent_name: str,
) -> bool:
    if not memory_agent_names:
        return False
    target = agent_name.strip().casefold()
    return any(str(n).strip().casefold() == target for n in memory_agent_names)


def _load_provider_settings(context: Context, umo: str) -> dict[str, Any]:
    cfg = context.get_config(umo=umo)
    if not isinstance(cfg, dict):
        return {}
    settings = cfg.get("provider_settings", {})
    return settings if isinstance(settings, dict) else {}


def _get_runtime_computer_tools(
    runtime: str,
    tool_mgr,
    provider_settings: dict[str, Any],
) -> dict[str, FunctionTool]:
    """Replicate FunctionToolExecutor._get_runtime_computer_tools without
    calling the private method."""
    if tool_mgr is None:
        return {}
    booter = ""
    sandbox_cfg = provider_settings.get("sandbox", {})
    if isinstance(sandbox_cfg, dict):
        booter = str(sandbox_cfg.get("booter", "") or "").lower()

    def _get(cls) -> FunctionTool | None:
        try:
            return tool_mgr.get_builtin_tool(cls)
        except Exception:  # noqa: BLE001
            return None

    try:
        from astrbot.core.tools import computer_tools as ct
    except ImportError:  # pragma: no cover
        return {}

    tools: dict[str, FunctionTool] = {}
    if runtime == "sandbox":
        for class_name in _SANDBOX_TOOL_CLASS_NAMES:
            cls = getattr(ct, class_name)
            tool = _get(cls)
            if tool is not None:
                tools[tool.name] = tool
        if booter == "cua":
            for class_name in _CUA_TOOL_CLASS_NAMES:
                cls = getattr(ct, class_name)
                tool = _get(cls)
                if tool is not None:
                    tools[tool.name] = tool
        return tools
    if runtime == "local":
        for class_name in _LOCAL_TOOL_CLASS_NAMES:
            cls = getattr(ct, class_name)
            tool = _get(cls)
            if tool is not None:
                tools[tool.name] = tool
        return tools
    return {}


# ---------------------------------------------------------------- memory dir


_UMO_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_path_segment(value: str) -> str:
    cleaned = _UMO_SAFE_RE.sub("_", value).strip("._")
    return cleaned or "default"


def get_memory_dir(unified_msg_origin: str, agent_name: str) -> Path:
    """Return the per-(UMO, agent) memory directory.

    Isolated from the runtime transcript dir and not deleted by retention.
    """
    base = StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME) / MEMORY_SUBDIR
    umo_digest = hashlib.sha256(unified_msg_origin.encode("utf-8")).hexdigest()[:12]
    umo_safe = f"{_sanitize_path_segment(unified_msg_origin)[:80]}-{umo_digest}"
    agent_safe = _sanitize_path_segment(agent_name)
    memory_dir = base / umo_safe / agent_safe
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir


def get_memory_index_path(unified_msg_origin: str, agent_name: str) -> Path:
    return get_memory_dir(unified_msg_origin, agent_name) / MEMORY_INDEX_FILENAME


def load_memory_index_inline(unified_msg_origin: str, agent_name: str) -> str | None:
    """Load MEMORY.md inline, capped at 200 lines / 25000 bytes.

    Returns the inline text, or None if no memory file exists. If the file
    exceeds the cap, a split hint is appended instead of the overflow.
    """
    path = get_memory_index_path(unified_msg_origin, agent_name)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[大小姐模式] 读取 MEMORY.md 失败: path=%s err=%s", path, exc)
        return None

    if len(raw.encode("utf-8")) <= MEMORY_MAX_BYTES:
        lines = raw.splitlines()
        if len(lines) <= MEMORY_MAX_LINES:
            return raw

    # Over cap: truncate to the line/byte limit and append a split hint.
    truncated_lines: list[str] = []
    total_bytes = 0
    for line in raw.splitlines():
        line_bytes = len((line + "\n").encode("utf-8"))
        if total_bytes + line_bytes > MEMORY_MAX_BYTES or len(truncated_lines) >= MEMORY_MAX_LINES:
            break
        truncated_lines.append(line)
        total_bytes += line_bytes
    truncated = "\n".join(truncated_lines)
    hint = (
        "\n\n[MEMORY.md 已超过内联上限（200 行 / 25000 bytes），仅显示前面部分。"
        "请将后续记忆拆分到独立的 topic 文件并在 MEMORY.md 中引用。]"
    )
    return truncated + hint
