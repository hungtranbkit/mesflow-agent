"""P3 Notification -- the two required vertical slices (sections 39/40/49),
run against the real sync_incidents()/NotificationDispatcher wiring (not
mocked), simulated entirely through synthetic condition tuples -- never by
touching the real production mesflow-postgres/mesflow-app containers.

Slice 1: MESFLOW_DOWN            (section 2's suggested first detector)
Slice 2: CONTAINER_DOWN:mesflow-postgres  (this Agent's actual detector for
         "the PostgreSQL container is not running" -- the closest existing
         equivalent to POSTGRES_UNAVAILABLE; there is no separate deep DB
         probe detector yet, only the container-level one built in the
         System Log/Audit Separation task)

Each slice proves the complete required lifecycle:
  outage -> ACTIVE incident -> notification -> sustained failure (multiple
  polls) -> no duplicate spam -> recovery -> recovery notification ->
  history preserved.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "PRODUCTION"  # prove environment labeling too (section 25/48)
    os.environ.pop("MESFLOW_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("MESFLOW_SMTP_HOST", None)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("agent_p3_slices", root / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_p3_slices"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_lifecycle(agent, condition_tuple, sustained_polls=5):
    fp = condition_tuple[0]
    # 1. outage begins -> ACTIVE incident opens
    items = agent.sync_incidents([condition_tuple])
    incident = items[fp]
    assert incident["status"] == "ACTIVE"
    agent._notification_dispatcher.reconcile(items)
    open_deliveries_1 = agent._notification_dispatcher.deliveries_for(incident["incident_id"])
    assert any(d["notification_type"] == "OPEN" and d["channel"] == "WEB" and d["status"] == "SENT" for d in open_deliveries_1)

    # 2. sustained failure across multiple detector cycles -> NO duplicate spam
    for _ in range(sustained_polls):
        items = agent.sync_incidents([condition_tuple])
        agent._notification_dispatcher.reconcile(items)
    web_opens = [d for d in agent._notification_dispatcher.deliveries_for(incident["incident_id"])
                 if d["channel"] == "WEB" and d["notification_type"] == "OPEN"]
    assert len(web_opens) == 1, f"expected exactly one OPEN/WEB delivery after {sustained_polls} repeated polls, got {len(web_opens)}"
    assert items[fp]["occurrence_count"] == sustained_polls + 1  # incident itself still tracks real recurrence

    # 3. condition recovers
    recovered_items = agent.sync_incidents([])
    assert recovered_items[fp]["status"] == "AUTO_RESOLVED"
    agent._notification_dispatcher.reconcile(recovered_items)

    # 4. repeated healthy polling -> no additional recovery spam
    for _ in range(3):
        agent._notification_dispatcher.reconcile(agent.sync_incidents([]))
    deliveries_final = agent._notification_dispatcher.deliveries_for(incident["incident_id"])
    web_recoveries = [d for d in deliveries_final if d["channel"] == "WEB" and d["notification_type"] == "RECOVERY"]
    assert len(web_recoveries) == 1
    assert web_recoveries[0]["status"] == "SENT"

    # 5. history preserved -- both the OPEN and RECOVERY records survive, the
    # incident itself is never deleted (section 38).
    return incident, deliveries_final


def test_slice_1_mesflow_down_full_lifecycle(agent):
    condition = ("MESFLOW_DOWN", "SERVICE_DOWN", "CRITICAL", "health-monitor", "prod-host-1", "mesflow",
                 "MESFlow application unreachable", "api_online=False, docker_running=False", {"mes_status": {"online": False}})
    incident, deliveries = _run_lifecycle(agent, condition)
    assert incident["incident_type"] == "SERVICE_DOWN"
    assert incident["severity"] == "CRITICAL"
    # CRITICAL routes to WEB+TELEGRAM+EMAIL (section 5); each gets its own
    # OPEN + RECOVERY record (SENT for WEB, SKIPPED/NOT_CONFIGURED for the
    # two unconfigured channels in this test env) -- 3 channels x 2 types.
    assert len(deliveries) == 6
    assert {(d["channel"], d["notification_type"]) for d in deliveries} == {
        ("WEB", "OPEN"), ("WEB", "RECOVERY"), ("TELEGRAM", "OPEN"), ("TELEGRAM", "RECOVERY"),
        ("EMAIL", "OPEN"), ("EMAIL", "RECOVERY"),
    }
    # environment labeling: SERVER_ROLE=PRODUCTION -> notifications are correctly PRODUCTION-scoped, never inferred from hostname.
    assert agent._notification_dispatcher.environment == "PRODUCTION"


def test_slice_2_postgres_container_down_full_lifecycle(agent):
    """Proves the notification engine is generic, not MESFLOW_DOWN-specific
    (section 40) -- CONTAINER_DOWN:mesflow-postgres is this Agent's actual
    detector standing in for POSTGRES_UNAVAILABLE (section 2: 'if some
    detectors do not exist yet, use those already implemented')."""
    condition = ("CONTAINER_DOWN:mesflow-postgres", "CONTAINER_DOWN", "HIGH", "docker", "prod-host-1", "mesflow-postgres",
                 "Container mesflow-postgres is not running", "state=exited status=Exited (1) 2 minutes ago", {"container": {"Names": "/mesflow-postgres", "State": "exited"}})
    incident, deliveries = _run_lifecycle(agent, condition)
    assert incident["incident_type"] == "CONTAINER_DOWN"
    assert incident["severity"] == "HIGH"
    # HIGH routes to WEB+TELEGRAM only (section 5) -- 2 channels x 2 types.
    assert len(deliveries) == 4
    assert {(d["channel"], d["notification_type"]) for d in deliveries} == {
        ("WEB", "OPEN"), ("WEB", "RECOVERY"), ("TELEGRAM", "OPEN"), ("TELEGRAM", "RECOVERY"),
    }


def test_two_incident_types_are_independent_and_do_not_cross_contaminate(agent):
    """Both slices active at once -- dedup/notification state must never
    bleed between different fingerprints."""
    mesflow_down = ("MESFLOW_DOWN", "SERVICE_DOWN", "CRITICAL", "health-monitor", "prod-host-1", "mesflow",
                     "MESFlow application unreachable", "api_online=False", {})
    pg_down = ("CONTAINER_DOWN:mesflow-postgres", "CONTAINER_DOWN", "HIGH", "docker", "prod-host-1", "mesflow-postgres",
               "Container mesflow-postgres is not running", "state=exited", {})
    items = agent.sync_incidents([mesflow_down, pg_down])
    agent._notification_dispatcher.reconcile(items)
    id_a, id_b = items["MESFLOW_DOWN"]["incident_id"], items["CONTAINER_DOWN:mesflow-postgres"]["incident_id"]
    assert id_a != id_b
    assert len(agent._notification_dispatcher.deliveries_for(id_a)) == 3  # CRITICAL: WEB+TELEGRAM+EMAIL, OPEN only so far
    assert len(agent._notification_dispatcher.deliveries_for(id_b)) == 2  # HIGH: WEB+TELEGRAM, OPEN only so far
    # resolve only the postgres one
    items2 = agent.sync_incidents([mesflow_down])
    agent._notification_dispatcher.reconcile(items2)
    assert len(agent._notification_dispatcher.deliveries_for(id_b)) == 4  # OPEN + RECOVERY x2 channels
    assert len(agent._notification_dispatcher.deliveries_for(id_a)) == 3  # still just OPEN x3 -- MESFLOW_DOWN never recovered
