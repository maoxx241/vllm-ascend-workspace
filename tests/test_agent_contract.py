import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.agent_contract import validate_action_kind, validate_tool_result


def test_probe_result_accepts_required_fields_only():
    result = validate_tool_result(
        {
            "status": "ready",
            "observations": ["host ssh ok"],
        },
        action_kind="probe",
    )
    assert result == {
        "status": "ready",
        "observations": ["host ssh ok"],
    }


def test_bootstrap_result_requires_side_effects():
    with pytest.raises(RuntimeError, match="side_effects"):
        validate_tool_result(
            {
                "status": "ready",
                "observations": ["runtime bootstrapped"],
            },
            action_kind="bootstrap",
        )


def test_execute_result_requires_side_effects():
    with pytest.raises(RuntimeError, match="side_effects"):
        validate_tool_result(
            {
                "status": "ready",
                "observations": ["benchmark finished"],
            },
            action_kind="execute",
        )


def test_execute_result_accepts_payload_mapping():
    result = validate_tool_result(
        {
            "status": "ready",
            "observations": ["service launched"],
            "side_effects": ["service session recorded"],
            "payload": {
                "service_id": "svc-123",
                "endpoint": "http://10.0.0.12:8000",
            },
        },
        action_kind="execute",
    )

    assert result["payload"] == {
        "service_id": "svc-123",
        "endpoint": "http://10.0.0.12:8000",
    }


def test_payload_must_be_a_mapping():
    with pytest.raises(RuntimeError, match="payload must be a mapping"):
        validate_tool_result(
            {
                "status": "ready",
                "observations": ["service launched"],
                "side_effects": ["service session recorded"],
                "payload": ["svc-123"],
            },
            action_kind="execute",
        )


def test_unknown_fields_are_rejected():
    with pytest.raises(RuntimeError, match="unexpected result keys"):
        validate_tool_result(
            {
                "status": "ready",
                "observations": ["ok"],
                "transport": "ssh",
            },
            action_kind="probe",
        )


def test_unknown_action_kind_is_rejected():
    with pytest.raises(RuntimeError, match="unsupported action_kind"):
        validate_action_kind("workflow")


def test_execute_action_kind_is_accepted():
    assert validate_action_kind("execute") == "execute"
