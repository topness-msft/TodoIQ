from src.services import skills, suggestion_checks
from tests.test_skill_workflow import store, runtime, finish


def test_merged_status_is_authoritative_for_skills_and_keeps_other_runs(store, monkeypatch):
    task = store()
    worker = skills.SkillService(runtime_provider=runtime)
    monkeypatch.setattr(skills, "_service", worker)
    admitted = worker.launch(task["id"], "prepare")
    terminal = finish(worker, admitted)
    label = f'skill:prepare:{task["id"]}'
    legacy = {label: True, "other": True, "_runs": {label: {"run_id": "old"}, "other": {"run_id": "other"}}}
    merged = skills.merged_status(legacy)
    assert label not in merged and label not in merged["_runs"]
    assert merged["other"] and merged["_runs"]["other"]["run_id"] == "other"
    completed = skills.merged_completions({label: {"run_id": "old"}, "other": {"state": "succeeded"}})
    assert completed[label] == terminal and "other" in completed
    assert suggestion_checks._completion(label) == terminal


def test_demo_blocks_direct_worker_before_provider_or_task_mutation(store, monkeypatch):
    task = store()
    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    worker = skills.SkillService(runtime_provider=runtime)
    for skill in skills.VALID_SKILLS:
        assert worker.launch(task["id"], skill)["ok"] is False
    assert worker.status() == {"_runs": {}}
    assert worker.completions() == {}
