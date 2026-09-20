import importlib
from unittest.mock import Mock

import pytest

from src import app, models
from src.services import parsing
from tests.test_refresh_workflow import store, worker, tables
from tests.test_parse_workflow import raw


@pytest.mark.parametrize("optin", [False, True])
def test_demo_only_explicit_parse_optin_never_refresh(store, monkeypatch, optin):
    task = store(parse_status="queued", parse_intent="coaching_only")
    service, parser, runtime = worker(monkeypatch)
    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_TODO_PARSE", "1" if optin else "0")
    before = tables()
    assert service.launch()["ok"] is False
    assert service.run()["state"] == "disabled"
    assert tables() == before and not runtime.calls
    result = parser.run()
    assert result["state"] == ("succeeded" if optin else "disabled")
    assert raw(task["id"])["parse_status"] == ("parsed" if optin else "queued")


@pytest.mark.parametrize("state", ["unparsed", "queued", "parsing", "error"])
def test_startup_recovery_preserves_intent_and_does_not_inference_loop(store, state):
    task = store(parse_status=state, parse_intent="coaching_only")
    app._recover_stuck_parses()
    after = raw(task["id"])
    assert after["parse_intent"] == "coaching_only"
    assert after["parse_status"] == ("error" if state == "parsing" else state)
    if state == "parsing":
        assert after["error_message"] == models.PARSE_FAILURE_MESSAGE


def test_parse_launch_adapts_legacy_metadata_without_task_content(store, monkeypatch):
    _, parser, _ = worker(monkeypatch)
    result = parser.launch([])
    parser.join(5)
    assert result["ok"] is True and result["run_id"] and result["started_at"]
    assert result["message"]
    assert parser.completion()["exit_code"] == 0 and parser.completion()["error"] is None


def test_parse_thread_start_failure_has_compatible_result_not_raw_exception(store, monkeypatch):
    _, parser, _ = worker(monkeypatch)
    monkeypatch.setattr(parsing.threading.Thread, "start", Mock(side_effect=RuntimeError("PRIVATE")))
    result = parser.launch([])
    assert result["ok"] is False and "PRIVATE" not in result["message"]
    assert parser.completion()["exit_code"] == 1
    parser.join(0)
