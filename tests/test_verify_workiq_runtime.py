import json
from unittest.mock import Mock

from scripts.verify_workiq_runtime import verify


def test_real_runtime_proof_emits_metadata_only_and_cannot_accept_eula():
    identity = "private-person@example.com"
    private_result = {"value": [{"scheduleId": identity, "scheduleItems": ["secret"]}]}
    setup = Mock(spec=["inspect"])
    setup.inspect.return_value = {
        "state": "mcp_unavailable",
        "installed_version": "1.0.0",
    }
    runtime = Mock()
    runtime.start.return_value = {
        "state": "ready",
        "protocol_version": "2025-06-18",
        "server": {"name": "WorkIQ", "version": "1.0.0"},
        "allowed_capabilities": ["do_action"],
    }
    runtime.probe.return_value = {"ok": True}
    runtime.snapshot.return_value = {"authenticated": True}
    reader = Mock(return_value=private_result)

    evidence = verify(setup, runtime, reader, identity)

    serialized = json.dumps(evidence)
    assert evidence["steps"]["calendar_read"] == "passed"
    assert identity not in serialized
    assert "scheduleItems" not in serialized
    assert "secret" not in serialized
    assert not hasattr(setup, "accept_eula")


def test_real_runtime_proof_rejects_invalid_schedule_result():
    identity = "private-person@example.com"
    setup = Mock(spec=["inspect"])
    setup.inspect.return_value = {
        "state": "mcp_unavailable",
        "installed_version": "1.0.0",
    }
    runtime = Mock()
    runtime.start.return_value = {
        "state": "ready",
        "protocol_version": "2025-06-18",
        "server": {"name": "WorkIQ", "version": "1.0.0"},
        "allowed_capabilities": ["do_action"],
    }
    runtime.probe.return_value = {"ok": True}
    runtime.snapshot.return_value = {"authenticated": True}

    for result in (
        {"value": []},
        {"value": [{"scheduleId": "other@example.com"}]},
        {
            "value": [{
                "scheduleId": identity,
                "error": {"code": "Synthetic"},
            }]
        },
        {
            "value": [{
                "scheduleId": identity,
                "error": {},
            }]
        },
        {
            "value": [
                {"scheduleId": identity},
                {"scheduleId": identity},
            ]
        },
    ):
        evidence = verify(
            setup,
            runtime,
            Mock(return_value=result),
            identity,
        )
        assert evidence["steps"]["calendar_read"] == "failed"
        assert evidence["error_code"] == "invalid_structured_content"
