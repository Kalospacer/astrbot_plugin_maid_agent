"""Strict plugin configuration and main-tool visibility tests."""

from __future__ import annotations

import pytest

from astrbot_plugin_maid_agent.config import ConfigValidationError, load_maid_mode_config, render_dispatch_prompt
from astrbot_plugin_maid_agent.constants import MAID_TOOL_NAMES
from astrbot_plugin_maid_agent.toolset_adapter import apply_main_tool_policy


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _Toolset:
    def __init__(self, *names: str):
        self.tools = [_Tool(name) for name in names]

    def get_tool(self, name: str):
        return next((tool for tool in self.tools if tool.name == name), None)

    def remove_tool(self, name: str):
        self.tools = [tool for tool in self.tools if tool.name != name]


def _names(toolset: _Toolset) -> set[str]:
    return {tool.name for tool in toolset.tools}


def test_hide_native_keeps_exactly_five_maid_tools():
    tools = _Toolset("send_message_to_user", *MAID_TOOL_NAMES, "transfer_to_butler", "web_search")
    apply_main_tool_policy(tools, hide_native_tools=True, hide_transfer_tools=True)
    assert _names(tools) == set(MAID_TOOL_NAMES)


def test_native_visible_keeps_maid_tools_and_removes_transfer_tools():
    tools = _Toolset("send_message_to_user", *MAID_TOOL_NAMES, "transfer_to_butler", "web_search")
    apply_main_tool_policy(tools, hide_native_tools=False, hide_transfer_tools=True)
    assert _names(tools) == {"send_message_to_user", *MAID_TOOL_NAMES, "web_search"}


def test_policy_does_not_mutate_unrelated_toolset():
    tools = _Toolset("send_message_to_user", "web_search")
    apply_main_tool_policy(tools, hide_native_tools=True, hide_transfer_tools=False)
    assert _names(tools) == {"send_message_to_user", "web_search"}


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"allowed_agent_names": []}, "allowed_agent_names"),
        ({"hide_native_tools": "true"}, "hide_native_tools"),
        ({"dispatch_session_mode": "eventual"}, "dispatch_session_mode"),
        ({"max_active_per_umo": 0}, "max_active_per_umo"),
        ({"dispatch_prompt_template": "{unknown}"}, "dispatch_prompt_template"),
        ({"memory_agent_names": ["other"]}, "memory_agent_names"),
    ],
)
def test_invalid_configuration_is_rejected_without_repair(patch, field):
    with pytest.raises(ConfigValidationError) as excinfo:
        load_maid_mode_config(patch)
    assert field in excinfo.value.errors


def test_valid_configuration_is_not_coerced_or_given_a_default_agent():
    cfg = load_maid_mode_config({"allowed_agent_names": ["worker"], "dispatch_session_mode": "foreground"})
    assert cfg.allowed_agent_names == ("worker",)
    assert not hasattr(cfg, "default_agent_name")
    assert cfg.dispatch_session_mode == "foreground"


def test_prompt_template_errors_at_render_time_instead_of_falling_back():
    with pytest.raises(ConfigValidationError):
        render_dispatch_prompt("{invalid}", true_user_input="u", request_text="p", include_raw_user_input=True)
