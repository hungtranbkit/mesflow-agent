"""Real incident found live (see reports/RELEASE_DEPLOY_UX_CLEANUP.md,
section "orphaned job reconciliation"): tutorial_job sat at status="running"
for 3+ days on the real Deploy Agent after an earlier restart, because
in-memory locks reset on every process start but the persisted status in
state.json does not self-heal for most job types. reconcile_orphaned_jobs_
on_startup() fixes every job-tracking key once, at Agent boot."""
import agent


def _set_job(key, status, **extra):
    def f(s):
        s[key] = {"status": status, "message": "in progress", **extra}
    agent.update_state(f)


def test_non_terminal_job_status_is_marked_interrupted_on_startup():
    _set_job("tutorial_job", "running")
    reconciled = agent.reconcile_orphaned_jobs_on_startup()
    assert ("tutorial_job", "running") in reconciled
    job = agent.load_state().get("tutorial_job")
    assert job["status"] == "INTERRUPTED"
    assert "restart" in job["message"].lower()
    assert job.get("finished_at")


def test_every_orphanable_job_key_gets_reconciled_when_non_terminal():
    non_terminal_samples = {
        "job": "deploying", "qa_job": "running", "tutorial_job": "running",
        "esp_tutorial_job": "publishing", "esp_builder_job": "BUILDING",
        "release_build_job": "BUILDING", "promote_test_job": "UPLOADING",
        "agent_update_job": "UPDATING", "agent_package_build_job": "BUILDING",
        "qa_release_build_job": "BUILDING",
        "qa_local_deploy_job": "DEPLOYING", "qa_promote_test_job": "VERIFYING",
        "qa_image_deploy_job": "running",
    }
    assert set(non_terminal_samples) == set(agent._ORPHANABLE_JOB_KEYS)
    for key, status in non_terminal_samples.items():
        _set_job(key, status)
    reconciled = agent.reconcile_orphaned_jobs_on_startup()
    assert {k for k, _ in reconciled} == set(non_terminal_samples)
    state = agent.load_state()
    for key in non_terminal_samples:
        assert state[key]["status"] == "INTERRUPTED"


def test_terminal_job_statuses_are_left_untouched():
    for key, status in (
        ("job", "success"), ("release_build_job", "SUCCESS"),
        ("agent_update_job", "FAILED"), ("promote_test_job", "ROLLED_BACK"),
        ("tutorial_job", "warning"),
    ):
        _set_job(key, status, sentinel="unchanged")
    reconciled = agent.reconcile_orphaned_jobs_on_startup()
    touched = {k for k, _ in reconciled}
    assert not touched & {"job", "release_build_job", "agent_update_job", "promote_test_job", "tutorial_job"}
    state = agent.load_state()
    assert state["job"]["status"] == "success"
    assert state["job"]["sentinel"] == "unchanged"


def test_missing_or_empty_job_keys_are_skipped_safely():
    def f(s):
        s.pop("job", None)
        s["qa_job"] = None
        s["tutorial_job"] = {}
    agent.update_state(f)
    reconciled = agent.reconcile_orphaned_jobs_on_startup()
    assert not any(k in ("job", "qa_job", "tutorial_job") for k, _ in reconciled)


def test_reconciliation_runs_at_agent_startup():
    import inspect
    source = inspect.getsource(agent.main)
    assert "reconcile_orphaned_jobs_on_startup()" in source
