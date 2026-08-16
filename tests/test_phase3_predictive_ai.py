from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from predictive import AIProvider, IncidentAIService, MetricStore, deterministic_suggestions, linear_forecast, recurring_failures, robust_anomaly, sanitize


def samples(values, hours=6):
    base = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    return [{"value": value, "sampled_at": datetime.fromtimestamp(base + index * hours * 3600, timezone.utc).isoformat()}
            for index, value in enumerate(values)]


def test_disk_forecast_stable_has_no_capacity_deadline():
    result = linear_forecast(samples([50] * 30), 100, min_samples=12, min_hours=24)
    assert result.status == "STABLE"
    assert result.thresholds["90"] is None


def test_disk_linear_growth_forecasts_threshold_explainably():
    result = linear_forecast(samples(range(40, 70)), 100, min_samples=12, min_hours=24)
    assert result.status == "FORECAST_AVAILABLE"
    assert 4.5 < result.thresholds["90"] < 5.5
    assert result.growth_per_day == pytest.approx(4.0)


def test_disk_growth_stops_clears_forecast_and_large_jump_is_filtered():
    stopped = list(range(40, 55)) + [54] * 25
    result = linear_forecast(samples(stopped), 100, min_samples=12, min_hours=24)
    assert result.thresholds["90"] is None or result.thresholds["90"] > 30
    noisy = list(range(40, 60)) + [95] + list(range(61, 80))
    filtered = linear_forecast(samples(noisy), 100, min_samples=12, min_hours=24)
    assert filtered.growth_per_day < 10


def test_forecast_requires_real_baseline():
    result = linear_forecast(samples([10, 11, 12]), 100, min_samples=12, min_hours=24)
    assert result.status == "INSUFFICIENT_DATA"
    assert result.confidence == "INSUFFICIENT_DATA"


def test_db_growth_anomaly_is_deterministic_and_single_spike_visible():
    normal = samples([100 + (index % 3) for index in range(30)] + [180])
    result = robust_anomaly(normal, min_samples=12)
    assert result["status"] == "ANOMALOUS"
    assert result["actual"] == 180
    assert result["expected_max"] < 180


def test_missing_anomaly_baseline_is_not_fabricated():
    assert robust_anomaly(samples([1, 2]), min_samples=12)["status"] == "INSUFFICIENT_DATA"


def test_recurring_failures_group_by_fingerprint_and_detect_time_pattern():
    cycles = [{"started_at": f"2026-08-{day:02d}T14:05:00Z", "resolved_at": f"2026-08-{day:02d}T14:15:00Z"} for day in range(8, 14)]
    rows = recurring_failures([{"fingerprint": "KIOSK_OFFLINE:KIOSK-07", "incident_type": "KIOSK_OFFLINE", "severity": "HIGH", "service": "KIOSK-07", "cycles": cycles}], now=datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp())
    assert rows[0]["incidents"] == 6
    assert rows[0]["time_pattern"] == {"hour_utc": 14, "matches": 6, "total": 6}
    assert rows[0]["fingerprint"] == "KIOSK_OFFLINE:KIOSK-07"


def test_same_alert_on_different_kiosk_remains_separate():
    incidents = [{"fingerprint": f"KIOSK_OFFLINE:{kiosk}", "severity": "HIGH", "cycles": [{"started_at": "2026-08-12T10:00:00Z"}, {"started_at": "2026-08-13T10:00:00Z"}]} for kiosk in ("A", "B")]
    assert len(recurring_failures(incidents, now=datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp())) == 2


def test_metric_store_retention_and_rollups(tmp_path):
    store = MetricStore(tmp_path / "metrics.json", raw_days=7, hourly_days=30, daily_days=365)
    for index in range(5):
        store.add("disk_used_bytes", index, "bytes", "root", sampled_at=f"2026-08-13T0{index}:00:00Z")
    payload = json.loads((tmp_path / "metrics.json").read_text())
    assert len(payload["raw"]) == 5
    assert len(payload["hourly"]) == 5
    assert payload["daily"][0]["sample_count"] == 5


class FakeProvider(AIProvider):
    name = "fake"
    model = "test"
    def __init__(self, payload): self.payload = payload; self.calls = 0; self.context = None
    def analyze(self, context): self.calls += 1; self.context = context; return self.payload


def valid_ai_payload():
    return {"summary": "Observed outage; cause remains uncertain.", "evidence": ["disk=82%"], "likely_causes": ["artifact growth"], "uncertainties": ["no IO latency"],
            "suggested_checks": [{"action": "Open disk diagnostics", "risk": "SAFE_CHECK", "reason": "disk trend", "evidence_refs": ["disk"]}]}


def test_ai_context_is_sanitized_structured_and_cached(tmp_path):
    provider = FakeProvider(valid_ai_payload()); service = IncidentAIService(tmp_path / "ai.json", provider)
    incident = {"incident_id": "i1", "evidence": {"password": "bad", "message": "Authorization: Bearer abc"}}
    first = service.analyze(incident, {}, []); second = service.analyze(incident, {}, [])
    assert first["status"] == "SUCCESS" and second == first and provider.calls == 1
    encoded = json.dumps(provider.context)
    assert "bad" not in encoded and "Bearer abc" not in encoded and "[REDACTED]" in encoded


def test_ai_malformed_output_fails_without_health_dependency(tmp_path):
    service = IncidentAIService(tmp_path / "ai.json", FakeProvider({"freeform": "restart everything"}))
    result = service.analyze({"incident_id": "i1"}, {}, [])
    assert result["status"] == "INVALID_OUTPUT"


def test_ai_high_risk_text_is_advice_only_and_has_no_execution_contract(tmp_path):
    payload = valid_ai_payload();payload["suggested_checks"] = [{"action": "restart PostgreSQL", "risk": "HIGH_RISK_ACTION", "reason": "hypothesis"}]
    service = IncidentAIService(tmp_path / "ai.json", FakeProvider(payload))
    result = service.analyze({"incident_id": "i1"}, {}, [])
    assert result["result"]["suggested_checks"][0]["risk"] == "HIGH_RISK_ACTION"
    source = Path(__file__).resolve().parents[1].joinpath("predictive.py").read_text()
    assert "import subprocess" not in source and "docker restart" not in source and "os.system(" not in source


def test_deterministic_remediation_is_safe_check_only():
    suggestions = deterministic_suggestions({"type": "DISK_CAPACITY_RISK"})
    assert suggestions and {x["risk"] for x in suggestions} == {"SAFE_CHECK"}


def test_sanitizer_redacts_credentials():
    cleaned = sanitize({"api_token": "abc", "url": "postgresql://user:pw@db/prod"})
    assert cleaned == {"api_token": "[REDACTED]", "url": "[REDACTED]"}


def test_agent_exposes_predictive_capabilities_and_bounded_endpoints():
    source = Path(__file__).resolve().parents[1].joinpath("agent.py").read_text()
    for capability in ("predictive_health", "incident_ai_advisory"):
        assert capability in source
    for route in ("/api/ops/predictions", "/api/ops/metrics/<metric>/trend", "/api/incidents/<incident_id>/ai-analysis"):
        assert route in source
    assert "start_predictive_monitor()" in source
    assert "PREDICTIVE_SAMPLE_SECONDS" in source


def test_operations_ui_keeps_current_health_above_predictive_content():
    html = Path(__file__).resolve().parents[1].joinpath("templates", "ops.html").read_text()
    assert html.index('id="score"') < html.index('id="predictiveInsights"')
    # UI polish pass translated the predictive badge text to Vietnamese
    # ("DỰ BÁO") for consistency with the rest of the page -- this class
    # name ("predicted") is what actually matters structurally.
    assert 'class="predicted"' in html
    assert "DỰ BÁO" in html
    assert "AI Analysis" in html
    assert "Suggested" not in html or "onclick=\"dockerAction" not in html[html.index("AI Analysis"):]


def test_version_is_bumped_consistently_for_deployable_agent_change():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION.txt").read_text().strip()
    image_version = version.removesuffix("-docker-runtime")
    assert f'AGENT_VERSION="{version}"' in (root / "agent.py").read_text()
    assert f"mesflow-deploy-agent:{image_version}" in (root / "docker" / "compose.linux.yml").read_text()
