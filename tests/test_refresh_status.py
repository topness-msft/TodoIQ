import importlib
import json
from unittest.mock import Mock

import pytest

from src.services import parsing
from src.services.suggestion_checks import SuggestionCheckQueue
from tests.test_refresh_workflow import store, worker
from tests.test_suggestion_recheck import PostSyncHarness, RUN_A, _failed_task


def test_direct_sync_parse_status_overlays_stale_legacy_and_preserves_skills(store, monkeypatch):
    refresh = importlib.import_module("src.services.refresh")
    service, parser, _ = worker(monkeypatch)
    legacy = {"sync": True, "parse": True, "skill:prepare:7": True,
              "_runs": {"sync": {"run_id": "old"}, "parse": {"run_id": "old"},
                        "skill:prepare:7": {"run_id": "skill"}}}
    idle = refresh.merged_status(legacy)
    assert "sync" not in idle and "parse" not in idle
    assert idle["_runs"] == {"skill:prepare:7": {"run_id": "skill"}}
    assert idle["skill:prepare:7"]
    service.run()
    completed = refresh.merged_completions({"sync": {"private": "old"}, "parse": {"private": "old"},
                                           "skill:prepare:7": {"run_id": "skill"}})
    assert completed["sync"] == service.completion()
    assert completed["parse"] == parser.completion()
    assert completed["skill:prepare:7"] == {"run_id": "skill"}
    assert "private" not in json.dumps(completed)


@pytest.mark.parametrize("fault", ["uuid", "missing_uuid", "state", "timestamp", "none"])
def test_direct_postpull_requires_exact_marker_uuid_and_success(store, monkeypatch, fault):
    h = PostSyncHarness()
    h.tasks[1] = _failed_task(1)
    h.failed_ids = [1]
    queue = h.queue()
    h.initialize(queue)
    h.complete_sync(RUN_A)
    h.marker["result_summary"] = json.dumps({"run_id": RUN_A})
    h.completions["sync"]["state"] = "succeeded"
    if fault == "uuid":
        h.marker["result_summary"] = '{"run_id":"33333333-3333-4333-8333-333333333333"}'
    elif fault == "missing_uuid":
        h.marker["result_summary"] = "{}"
    elif fault == "state":
        h.completions["sync"]["state"] = "partial"
    elif fault == "timestamp":
        h.marker["synced_at"] = "2026-09-10T14:01:00.999999Z"
    queue.pump_once()
    queue.pump_once()
    assert h.launches == ([1] if fault == "none" else [])


def test_browserless_default_observer_reads_direct_completion_not_legacy(store, monkeypatch):
    service, _, _ = worker(monkeypatch)
    from src.services import suggestion_checks
    legacy = Mock(side_effect=AssertionError("Not a sync process"))
    monkeypatch.setattr(suggestion_checks, "get_exit_info", legacy)
    assert suggestion_checks._completion("sync") is None
    service.run()
    assert suggestion_checks._completion("sync") == service.completion()
    legacy.assert_not_called()
