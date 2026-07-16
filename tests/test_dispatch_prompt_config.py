from __future__ import annotations

from types import SimpleNamespace

import astrbot_plugin_maid_agent.config as config_module
from astrbot_plugin_maid_agent.config import (
    DEFAULT_DISPATCH_PROMPT_TEMPLATE,
    load_maid_mode_config,
    render_dispatch_prompt,
)


def _capture_warnings(monkeypatch):
    warnings: list[tuple] = []
    monkeypatch.setattr(
        config_module,
        "logger",
        SimpleNamespace(warning=lambda *args: warnings.append(args)),
    )
    config_module._warned_invalid_prompt_templates.clear()
    return warnings


def test_removed_full_reply_placeholder_falls_back_to_default(monkeypatch) -> None:
    warnings = _capture_warnings(monkeypatch)
    template = (
        "{user_input_block}{maid_full_reply_block}{maid_request_block}执行以上任务"
    )

    rendered = render_dispatch_prompt(
        template,
        user_input_block="USER\n",
        maid_request_block="REQUEST\n",
    )
    expected = DEFAULT_DISPATCH_PROMPT_TEMPLATE.format_map(
        {
            "user_input_block": "USER\n",
            "maid_request_block": "REQUEST\n",
        }
    ).strip()

    assert rendered == expected
    assert len(warnings) == 1
    assert "maid_full_reply_block" in str(warnings[0])


def test_current_dispatch_prompt_template_renders_normally(monkeypatch) -> None:
    warnings = _capture_warnings(monkeypatch)

    rendered = render_dispatch_prompt(
        "{user_input_block}{maid_request_block}执行",
        user_input_block="USER\n",
        maid_request_block="REQUEST\n",
    )

    assert rendered == "USER\nREQUEST\n执行"
    assert warnings == []


def test_unknown_placeholder_falls_back_to_default_template(monkeypatch) -> None:
    warnings = _capture_warnings(monkeypatch)

    rendered = render_dispatch_prompt(
        "{user_input_block}{unknown_block}{maid_request_block}",
        user_input_block="USER\n",
        maid_request_block="REQUEST\n",
    )
    expected = DEFAULT_DISPATCH_PROMPT_TEMPLATE.format_map(
        {
            "user_input_block": "USER\n",
            "maid_request_block": "REQUEST\n",
        }
    ).strip()

    assert rendered == expected
    assert len(warnings) == 1
    assert "unknown_block" in str(warnings[0])


def test_invalid_template_warning_is_emitted_once(monkeypatch) -> None:
    warnings = _capture_warnings(monkeypatch)

    for _ in range(2):
        render_dispatch_prompt(
            "{broken",
            user_input_block="USER\n",
            maid_request_block="REQUEST\n",
        )

    assert len(warnings) == 1


def test_config_loader_does_not_mutate_original_mapping() -> None:
    raw_config = {
        "dispatch_prompt_template": (
            "{user_input_block}{maid_full_reply_block}{maid_request_block}"
        ),
        "allowed_agent_names": ["muiceagent"],
    }
    snapshot = {
        "dispatch_prompt_template": raw_config["dispatch_prompt_template"],
        "allowed_agent_names": list(raw_config["allowed_agent_names"]),
    }

    loaded = load_maid_mode_config(raw_config)

    assert raw_config == snapshot
    assert loaded.dispatch_prompt_template == raw_config["dispatch_prompt_template"]
