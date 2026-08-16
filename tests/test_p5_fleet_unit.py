"""P5 Multi-server -- ServerRegistryService / classify_health() /
FleetHealthService, pure unit tests (no Flask, no real HTTP)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_backend.fleet import (
    DuplicateAgentIdentity,
    FleetHealthService,
    ProductionConfirmationRequired,
    RemoteCallResult,
    ServerRegistryService,
    classify_health,
    new_correlation_id,
    new_server_id,
)


@pytest.fixture()
def registry(tmp_path):
    events = []
    reg = ServerRegistryService(tmp_path / "servers.json", audit=lambda a, d: events.append((a, d)))
    reg._events = events
    return reg


def test_register_defaults_to_pending_and_unknown_health(registry):
    rec = registry.register(name="mesflow-test", environment="TEST", roles=["MESFLOW", "POSTGRESQL"],
                             agent_url="http://test:8080", agent_user="admin", agent_password="secret")
    assert rec["registration_status"] == "PENDING"
    assert rec["health_status"] == "UNKNOWN"
    assert rec["server_id"].startswith("srv-")
    assert "agent_password" not in rec  # never returned, even right after registering


def test_register_rejects_unknown_environment(registry):
    with pytest.raises(ValueError):
        registry.register(name="x", environment="STAGING")


def test_register_production_requires_explicit_confirmation(registry):
    with pytest.raises(ProductionConfirmationRequired):
        registry.register(name="mesflow-prod", environment="PRODUCTION")
    # with confirmation, it succeeds
    rec = registry.register(name="mesflow-prod", environment="PRODUCTION", production_confirmed=True)
    assert rec["environment"] == "PRODUCTION"
    assert any(a == "SERVER_REGISTERED" and d["environment"] == "PRODUCTION" for a, d in registry._events)


def test_credential_never_leaks_through_get_or_list(registry):
    registry.register(name="s1", environment="TEST", agent_password="topsecret")
    for rec in registry.list():
        assert "agent_password" not in rec
    sid = registry.list()[0]["server_id"]
    assert "agent_password" not in registry.get(sid)
    # but it IS actually stored internally (needed for repeated remote calls)
    raw = registry._load()["items"][sid]
    assert raw["agent_password"] == "topsecret"


def test_handshake_activates_and_records_identity(registry):
    rec = registry.register(name="mesflow-test", environment="TEST")
    sid = rec["server_id"]
    updated = registry.record_handshake(sid, {"agent_id": "agent-abc", "agent_version": "2.23.8",
                                                "capabilities": ["health_status", "remote_deploy"]})
    assert updated["registration_status"] == "ACTIVE"
    assert updated["agent_id"] == "agent-abc"
    assert updated["capabilities"] == ["health_status", "remote_deploy"]
    assert updated["last_seen"]


def test_duplicate_agent_identity_rejected(registry):
    a = registry.register(name="server-a", environment="TEST")["server_id"]
    b = registry.register(name="server-b", environment="TEST")["server_id"]
    registry.record_handshake(a, {"agent_id": "same-agent", "agent_version": "1.0"})
    with pytest.raises(DuplicateAgentIdentity):
        registry.record_handshake(b, {"agent_id": "same-agent", "agent_version": "1.0"})


def test_handshake_failure_marks_unreachable_and_offline(registry):
    sid = registry.register(name="mesflow-test", environment="TEST")["server_id"]
    registry.record_handshake(sid, {"agent_id": "a1", "agent_version": "1.0"})
    assert registry.get(sid)["registration_status"] == "ACTIVE"
    registry.record_handshake_failure(sid, "connection refused")
    rec = registry.get(sid)
    assert rec["registration_status"] == "UNREACHABLE"
    assert rec["health_status"] == "OFFLINE"
    assert rec["last_error"] == "connection refused"


def test_disable_never_deletes_history(registry):
    sid = registry.register(name="mesflow-test", environment="TEST")["server_id"]
    registry.record_handshake(sid, {"agent_id": "a1", "agent_version": "1.0"})
    registry.disable(sid, actor="admin")
    rec = registry.get(sid)
    assert rec["registration_status"] == "DISABLED"
    assert rec["enabled"] is False
    assert rec["agent_id"] == "a1"  # history preserved, not wiped
    assert any(s["server_id"] == sid for s in registry.list())  # still listed


def test_enable_goes_back_to_pending_not_directly_active(registry):
    sid = registry.register(name="mesflow-test", environment="TEST")["server_id"]
    registry.record_handshake(sid, {"agent_id": "a1", "agent_version": "1.0"})
    registry.disable(sid)
    registry.enable(sid)
    assert registry.get(sid)["registration_status"] == "PENDING"


def test_revoke_is_distinct_from_disable(registry):
    sid = registry.register(name="x", environment="TEST")["server_id"]
    registry.revoke(sid, actor="admin")
    assert registry.get(sid)["registration_status"] == "REVOKED"


def test_list_filters_by_environment_status_enabled(registry):
    registry.register(name="dev", environment="DEV")
    t = registry.register(name="test", environment="TEST")["server_id"]
    registry.register(name="prod", environment="PRODUCTION", production_confirmed=True)
    registry.disable(t)
    assert {s["name"] for s in registry.list(environment="TEST")} == {"test"}
    assert {s["name"] for s in registry.list(status="DISABLED")} == {"test"}
    assert {s["name"] for s in registry.list(enabled=False)} == {"test"}
    assert {s["name"] for s in registry.list(enabled=True)} == {"dev", "prod"}


# -- classify_health ---------------------------------------------------

def test_classify_health_unknown_when_never_seen():
    assert classify_health(None) == "UNKNOWN"
    assert classify_health("not-a-timestamp") == "UNKNOWN"


def test_classify_health_boundaries():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(seconds=10)).isoformat()
    degraded = (now - timedelta(seconds=200)).isoformat()
    offline = (now - timedelta(seconds=1000)).isoformat()
    assert classify_health(fresh, now=now, fresh_seconds=90, stale_seconds=300) == "ONLINE"
    assert classify_health(degraded, now=now, fresh_seconds=90, stale_seconds=300) == "DEGRADED"
    assert classify_health(offline, now=now, fresh_seconds=90, stale_seconds=300) == "OFFLINE"


def test_correlation_and_server_ids_are_unique():
    assert new_correlation_id() != new_correlation_id()
    assert new_server_id() != new_server_id()
    assert new_server_id().startswith("srv-")


# -- FleetHealthService --------------------------------------------------

def test_fleet_health_service_isolates_one_failure_from_the_rest(registry):
    a = registry.register(name="server-a", environment="TEST")["server_id"]
    b = registry.register(name="server-b", environment="TEST")["server_id"]
    c = registry.register(name="server-c", environment="PRODUCTION", production_confirmed=True)["server_id"]
    for sid in (a, b, c):
        registry.record_handshake(sid, {"agent_id": sid, "agent_version": "1.0"})

    def poll_fn(server):
        if server["server_id"] == b:
            raise TimeoutError("boom")  # simulate an offline/misbehaving agent
        return RemoteCallResult(ok=True, server_id=server["server_id"], operation="health", data={"mes": {"online": True}})

    svc = FleetHealthService(registry, poll_fn)
    results = svc.poll_once()
    assert results[a].ok is True
    assert results[b].ok is False
    assert results[c].ok is True
    # the registry itself must reflect this too -- one bad node never
    # blocks the others' state from updating (section 46).
    assert registry.get(a)["health_status"] == "ONLINE"
    assert registry.get(b)["health_status"] == "OFFLINE"
    assert registry.get(b)["registration_status"] == "UNREACHABLE"
    assert registry.get(c)["health_status"] == "ONLINE"


def test_fleet_health_service_skips_disabled_servers(registry):
    sid = registry.register(name="x", environment="TEST")["server_id"]
    registry.record_handshake(sid, {"agent_id": sid, "agent_version": "1.0"})
    registry.disable(sid)
    calls = []

    def poll_fn(server):
        calls.append(server["server_id"])
        return RemoteCallResult(ok=True, server_id=server["server_id"], operation="health")

    FleetHealthService(registry, poll_fn).poll_once()
    assert calls == []


def test_fleet_summary_counts_are_correct(registry):
    a = registry.register(name="a", environment="DEV")["server_id"]
    b = registry.register(name="b", environment="TEST")["server_id"]
    c = registry.register(name="c", environment="PRODUCTION", production_confirmed=True)["server_id"]
    for sid, health in ((a, "ONLINE"), (b, "DEGRADED"), (c, "OFFLINE")):
        registry.record_handshake(sid, {"agent_id": sid, "agent_version": "1.0"})
        registry.update_health_cache(sid, health, {})
    svc = FleetHealthService(registry, poll_fn=lambda s: RemoteCallResult(ok=True, server_id=s["server_id"], operation="health"))
    summary = svc.fleet_summary()
    assert summary["total"] == 3
    assert summary["online"] == 1
    assert summary["degraded"] == 1
    assert summary["offline"] == 1
    assert summary["production"] == 1
    assert summary["enabled"] == 3


def test_fleet_health_service_reactivates_previously_unreachable_server(registry):
    sid = registry.register(name="x", environment="TEST")["server_id"]
    registry.record_handshake(sid, {"agent_id": sid, "agent_version": "1.0"})
    registry.record_handshake_failure(sid, "timeout")
    assert registry.get(sid)["registration_status"] == "UNREACHABLE"

    svc = FleetHealthService(registry, poll_fn=lambda s: RemoteCallResult(
        ok=True, server_id=s["server_id"], operation="health",
        data={"agent_id": sid, "agent_version": "1.0", "capabilities": ["health_status"]}))
    svc.poll_once()
    rec = registry.get(sid)
    assert rec["registration_status"] == "ACTIVE"
    assert rec["health_status"] == "ONLINE"
