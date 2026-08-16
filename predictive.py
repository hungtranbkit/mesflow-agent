"""Explainable predictive analytics for the Deploy Agent Operations Center.

The module is deliberately pure/read-only: it stores bounded observations and
returns evidence/advice.  It contains no subprocess, Docker, SSH, deployment or
service-control path, so AI output can never become an infrastructure command.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import threading
import urllib.request


def _utc_text(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts is not None else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: str | float | int) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp() if "+" not in str(value) and not str(value).endswith("Z") else datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class Forecast:
    status: str
    current_value: float | None
    growth_per_day: float | None
    confidence: str
    reason: str
    sample_count: int
    observation_hours: float
    thresholds: dict[str, float | None]
    expected_30d: float | None = None


class MetricStore:
    """Compact JSON sample store with high-resolution retention and rollups."""

    def __init__(self, path: Path, raw_days: int = 14, hourly_days: int = 90, daily_days: int = 365):
        self.path = Path(path)
        self.raw_seconds = max(1, raw_days) * 86400
        self.hourly_seconds = max(raw_days, hourly_days) * 86400
        self.daily_seconds = max(hourly_days, daily_days) * 86400
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"raw": [], "hourly": [], "daily": []}

    def _save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    @staticmethod
    def _rollup(rows: list[dict], bucket_seconds: int) -> list[dict]:
        groups: dict[tuple[str, str, int], list[float]] = {}
        for row in rows:
            bucket = int(_epoch(row["sampled_at"]) // bucket_seconds) * bucket_seconds
            groups.setdefault((row["metric"], row.get("component_id", ""), bucket), []).append(float(row["value"]))
        return [{"metric": metric, "component_id": component, "sampled_at": _utc_text(bucket),
                 "min": min(values), "max": max(values), "avg": statistics.fmean(values),
                 "median": statistics.median(values), "sample_count": len(values)}
                for (metric, component, bucket), values in sorted(groups.items())]

    @staticmethod
    def _merge_rollups(previous: list[dict], current: list[dict]) -> list[dict]:
        merged={(x.get("metric"),x.get("component_id",""),x.get("sampled_at")):x for x in previous}
        for row in current:merged[(row.get("metric"),row.get("component_id",""),row.get("sampled_at"))]=row
        return list(merged.values())

    def add(self, metric: str, value: float, unit: str, component_id: str = "", metadata: dict | None = None, sampled_at: str | None = None) -> dict:
        row = {"metric": str(metric), "component_id": str(component_id), "value": float(value), "unit": str(unit),
               "sampled_at": sampled_at or _utc_text(), "metadata": metadata or {}}
        with self._lock:
            data = self._load()
            raw = list(data.get("raw", [])); raw.append(row)
            now = _epoch(row["sampled_at"])
            raw = [item for item in raw if _epoch(item["sampled_at"]) >= now - self.raw_seconds]
            # Rollups are rebuilt from bounded raw data. This is intentionally
            # small-scale and deterministic, not a monitoring database.
            hourly = self._merge_rollups(list(data.get("hourly",[])),self._rollup(raw, 3600))
            daily = self._merge_rollups(list(data.get("daily",[])),self._rollup(raw, 86400))
            data = {"raw": raw,
                    "hourly": [x for x in hourly if _epoch(x["sampled_at"]) >= now - self.hourly_seconds],
                    "daily": [x for x in daily if _epoch(x["sampled_at"]) >= now - self.daily_seconds]}
            self._save(data)
        return row

    def samples(self, metric: str, component_id: str | None = "", since: float | None = None) -> list[dict]:
        with self._lock:
            rows = list(self._load().get("raw", []))
        result = [x for x in rows if x.get("metric") == metric and (component_id is None or x.get("component_id", "") == component_id)]
        if since is not None:
            result = [x for x in result if _epoch(x["sampled_at"]) >= since]
        return sorted(result, key=lambda x: _epoch(x["sampled_at"]))


def _robust_rows(samples: list[dict]) -> list[tuple[float, float]]:
    rows = sorted((_epoch(x["sampled_at"]), float(x["value"])) for x in samples if math.isfinite(float(x["value"])))
    if len(rows) < 5:
        return rows
    diffs = [(rows[i][1] - rows[i - 1][1]) / max(1.0, rows[i][0] - rows[i - 1][0]) for i in range(1, len(rows))]
    median = statistics.median(diffs); mad = statistics.median(abs(x - median) for x in diffs)
    if mad == 0:
        return rows
    keep = [rows[0]]
    for index, row in enumerate(rows[1:]):
        if abs(diffs[index] - median) <= 6 * mad:
            keep.append(row)
    return keep


def linear_forecast(samples: list[dict], capacity: float | None = None, thresholds: tuple[float, ...] = (.8, .9, 1.0), min_samples: int = 12, min_hours: float = 24) -> Forecast:
    rows = _robust_rows(samples)
    if len(rows) < min_samples:
        return Forecast("INSUFFICIENT_DATA", rows[-1][1] if rows else None, None, "INSUFFICIENT_DATA", f"Cần ít nhất {min_samples} mẫu sử dụng được.", len(rows), 0, {})
    hours = (rows[-1][0] - rows[0][0]) / 3600
    if hours < min_hours:
        return Forecast("INSUFFICIENT_DATA", rows[-1][1], None, "INSUFFICIENT_DATA", f"Cần tối thiểu {min_hours:g} giờ lịch sử.", len(rows), hours, {})
    x0 = rows[0][0]; xs = [(x - x0) / 86400 for x, _ in rows]; ys = [y for _, y in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    predicted = [my + slope * (x - mx) for x in xs]
    residuals = [y - p for y, p in zip(ys, predicted)]
    variance = statistics.pstdev(residuals) / max(abs(statistics.fmean(ys)), 1.0)
    confidence = "HIGH" if len(rows) >= 48 and hours >= 168 and variance <= .02 else "MEDIUM" if len(rows) >= 24 and hours >= 48 and variance <= .08 else "LOW"
    estimates: dict[str, float | None] = {}
    for threshold in thresholds:
        target = capacity * threshold if capacity is not None else threshold
        estimates[str(int(threshold * 100))] = max(0.0, (target - ys[-1]) / slope) if slope > 0 else None
    status = "STABLE" if slope <= 0 else "FORECAST_AVAILABLE"
    return Forecast(status, ys[-1], slope, confidence, "Hồi quy tuyến tính sau lọc biến động ngoại lai; thời điểm là ước lượng.", len(rows), hours, estimates, ys[-1] + slope * 30)


def robust_anomaly(samples: list[dict], current: float | None = None, min_samples: int = 12, z_threshold: float = 4.0) -> dict:
    values = [float(x["value"]) for x in samples if math.isfinite(float(x["value"]))]
    if len(values) < min_samples:
        return {"status": "INSUFFICIENT_DATA", "sample_count": len(values), "confidence": "INSUFFICIENT_DATA"}
    actual = float(values[-1] if current is None else current); baseline = values[:-1] or values
    median = statistics.median(baseline); mad = statistics.median(abs(x - median) for x in baseline)
    spread = max(mad * 1.4826, max(abs(median) * .01, 1e-9)); deviation = abs(actual - median) / spread
    anomalous = deviation >= z_threshold
    return {"status": "ANOMALOUS" if anomalous else "NORMAL", "actual": actual,
            "expected_min": median - z_threshold * spread, "expected_max": median + z_threshold * spread,
            "baseline_median": median, "deviation": deviation, "sample_count": len(values),
            "confidence": "HIGH" if len(values) >= 48 else "MEDIUM"}


def recurring_failures(incidents: list[dict], now: float | None = None, window_days: int = 30) -> list[dict]:
    now = now or datetime.now(timezone.utc).timestamp(); since = now - window_days * 86400
    groups: dict[str, list[dict]] = {}
    for incident in incidents:
        fingerprint = str(incident.get("fingerprint") or "")
        cycles = incident.get("cycles") or [{"started_at": incident.get("started_at"), "resolved_at": incident.get("resolved_at")}]
        for cycle in cycles:
            try:
                if cycle.get("started_at") and _epoch(cycle["started_at"]) >= since:
                    groups.setdefault(fingerprint, []).append({**cycle, "incident": incident})
            except (TypeError, ValueError):
                continue
    result = []
    weights = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    for fingerprint, cycles in groups.items():
        if len(cycles) < 2:
            continue
        hours = [datetime.fromtimestamp(_epoch(x["started_at"]), timezone.utc).hour for x in cycles]
        common_hour = max(set(hours), key=hours.count); common_count = hours.count(common_hour)
        recent = sum(_epoch(x["started_at"]) >= now - 7 * 86400 for x in cycles)
        previous = sum(now - 14 * 86400 <= _epoch(x["started_at"]) < now - 7 * 86400 for x in cycles)
        trend = "INCREASING" if recent > previous else "DECREASING" if recent < previous else "STABLE"
        severity = str(cycles[-1]["incident"].get("severity") or "LOW")
        score = min(100, len(cycles) * 8 + recent * 6 + weights.get(severity, 1) * 8)
        result.append({"fingerprint": fingerprint, "incident_type": cycles[-1]["incident"].get("incident_type"),
                       "service": cycles[-1]["incident"].get("service"), "severity": severity,
                       "incidents": len(cycles), "recent_7d": recent, "trend": trend, "score": score,
                       "risk": "HIGH" if score >= 65 else "MEDIUM" if score >= 35 else "LOW",
                       "time_pattern": {"hour_utc": common_hour, "matches": common_count, "total": len(cycles)} if common_count >= max(2, math.ceil(len(cycles) * .5)) else None})
    return sorted(result, key=lambda x: (x["score"], x["incidents"]), reverse=True)


_SECRET_KEY = re.compile(r"password|secret|token|cookie|authorization|api[_-]?key|credential", re.I)
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+\S+|postgres(?:ql)?://[^\s]+|password\s*[=:]\s*\S+)")


def sanitize(value, depth: int = 0):
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else sanitize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(x, depth + 1) for x in value[:100]]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value[:8000])
    return value


class AIProvider:
    name = "abstract"
    model = ""
    def analyze(self, context: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class DisabledAIProvider(AIProvider):
    name = "disabled"
    def analyze(self, context: dict) -> dict:
        raise RuntimeError("AI_DISABLED")


class HTTPJSONAIProvider(AIProvider):
    """Optional JSON-in/JSON-out provider; no provider-specific SDK required."""
    name = "http-json"
    def __init__(self, url: str, model: str = "", timeout: float = 12):
        self.url, self.model, self.timeout = url, model, timeout
    def analyze(self, context: dict) -> dict:
        body = json.dumps({"model": self.model, "context": context}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read(256_000).decode("utf-8"))


class IncidentAIService:
    allowed_risks = {"SAFE_CHECK", "LOW_RISK_ACTION", "HIGH_RISK_ACTION"}
    def __init__(self, cache_path: Path, provider: AIProvider | None = None, max_context_bytes: int = 48_000):
        self.cache_path = Path(cache_path); self.provider = provider or DisabledAIProvider(); self.max_context_bytes = max_context_bytes
    def _context(self, incident: dict, metrics: dict, recurring: list[dict]) -> dict:
        value = sanitize({"incident": incident, "metrics": metrics, "similar_incidents": recurring[:10]})
        encoded = json.dumps(value, ensure_ascii=False).encode()
        if len(encoded) > self.max_context_bytes:
            value["incident"]["evidence"] = {"note": "Evidence truncated to configured AI context limit"}
        return value
    @staticmethod
    def _validate(payload: dict) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
            raise ValueError("INVALID_OUTPUT")
        output = {"summary": payload["summary"][:4000], "evidence": list(payload.get("evidence") or [])[:20],
                  "likely_causes": list(payload.get("likely_causes") or [])[:10], "uncertainties": list(payload.get("uncertainties") or [])[:10], "suggested_checks": []}
        for item in list(payload.get("suggested_checks") or [])[:10]:
            if not isinstance(item, dict) or item.get("risk") not in IncidentAIService.allowed_risks or not isinstance(item.get("action"), str):
                continue
            output["suggested_checks"].append({"action": item["action"][:500], "risk": item["risk"], "reason": str(item.get("reason") or "")[:1000], "evidence_refs": list(item.get("evidence_refs") or [])[:10]})
        return output
    def analyze(self, incident: dict, metrics: dict, recurring: list[dict], force: bool = False) -> dict:
        context = self._context(incident, metrics, recurring)
        context_hash = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cache = {}
        try: cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError): pass
        key = str(incident.get("incident_id")); cached = cache.get(key)
        if cached and cached.get("context_hash") == context_hash and not force:
            return cached
        try:
            result = self._validate(self.provider.analyze(context)); status = "SUCCESS"; error = ""
        except TimeoutError:
            result = {}; status = "TIMEOUT"; error = "AI provider timed out"
        except ValueError as exc:
            result = {}; status = "INVALID_OUTPUT"; error = str(exc)
        except Exception as exc:
            result = {}; status = "DISABLED" if str(exc) == "AI_DISABLED" else "FAILED"; error = type(exc).__name__
        record = {"incident_id": key, "analysis_version": 1, "context_hash": context_hash,
                  "generated_at": _utc_text(), "provider": self.provider.name, "model": self.provider.model,
                  "status": status, "error": error, "result": result}
        cache[key] = record; self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(".tmp"); temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"); temp.replace(self.cache_path)
        return record


def deterministic_suggestions(insight: dict) -> list[dict]:
    kind = insight.get("type")
    if kind == "DISK_CAPACITY_RISK":
        return [{"action": "Mở chẩn đoán dung lượng và xem retention của QA/release/log", "risk": "SAFE_CHECK", "reason": "Xu hướng dung lượng đang tiến gần ngưỡng cấu hình.", "evidence_refs": ["disk_forecast"]}]
    if kind == "DB_GROWTH_ANOMALY":
        return [{"action": "Xem PostgreSQL logs và bảng tăng trưởng gần nhất", "risk": "SAFE_CHECK", "reason": "Tốc độ tăng hiện tại lệch khỏi baseline lịch sử.", "evidence_refs": ["db_growth", "db_size_anomaly"]}]
    if kind == "RECURRING_FAILURE":
        return [{"action": "Mở các sự cố tương tự và so sánh thời điểm xảy ra", "risk": "SAFE_CHECK", "reason": "Fingerprint này lặp lại trong cửa sổ theo dõi.", "evidence_refs": ["recurrence"]}]
    return []
