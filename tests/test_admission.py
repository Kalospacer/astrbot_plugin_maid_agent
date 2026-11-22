"""覆盖派发评估表单的解析与方向判定。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot_plugin_maid_agent.harness.admission import (
    AdmissionError,
    assess,
    parse_form,
)


def _base_form(**direction_overrides) -> dict:
    direction = {
        "target_basis": "specified",
        "requires_design_decision": False,
        "evidence": "用户明确要求排查 SnowLuma 状态。",
    }
    direction.update(direction_overrides)
    return {
        "rule_assessments": [],
        "direction": direction,
        "intent_evidence": "你让女仆查查",
        "scope": "只读检查 SnowLuma 运行状态，不改动配置。",
    }


def test_readonly_request_with_implementation_false_is_accepted():
    """只读排查把 implementation_requested 设为 false 时，目标明确即放行。"""
    form = parse_form(_base_form(implementation_requested=False))
    assert form.direction.implementation_requested is False
    assert form.direction.settled is True

    outcome = assess(form, rules=())
    assert outcome.accepted is True
    assert outcome.reason == ""


def test_omitting_implementation_requested_defaults_false_and_accepts():
    """省略 implementation_requested 时默认 False 并放行；传非布尔仍报错。"""
    form = parse_form(_base_form())
    assert form.direction.implementation_requested is False
    assert assess(form, rules=()).accepted is True

    with pytest.raises(AdmissionError, match="implementation_requested"):
        parse_form(_base_form(implementation_requested="false"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_basis": "undetermined"},
        {"requires_design_decision": True},
        {"implementation_requested": True, "target_basis": "undetermined"},
        {"implementation_requested": True, "requires_design_decision": True},
    ],
)
def test_unsettled_direction_still_blocks(overrides):
    """目标未定或仍需设计拍板时，依然返回 directionUnsettled。"""
    form = parse_form(_base_form(**overrides))
    assert form.direction.settled is False

    outcome = assess(form, rules=())
    assert outcome.accepted is False
    assert outcome.reason == "directionUnsettled"


def test_enabled_rules_still_enforced():
    """有启用规则时，仍按顺序校验规则命中与意图证据。"""
    rule = SimpleNamespace(id="check-service", enabled=True)
    raw = _base_form(implementation_requested=False)
    raw["rule_assessments"] = [
        {"rule_id": "check-service", "matched": False, "reason": "未命中"}
    ]
    outcome = assess(parse_form(raw), rules=(rule,))
    assert outcome.accepted is False
    assert outcome.reason == "noRuleMatched"

    raw["rule_assessments"][0]["matched"] = True
    outcome = assess(parse_form(raw), rules=(rule,))
    assert outcome.accepted is True
    assert outcome.matched == ("check-service",)
