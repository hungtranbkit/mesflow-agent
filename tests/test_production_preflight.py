"""Production preflight: read-only checks against a fake target Agent (real
HTTP server, real socket), plus the Promote Production gate's requirement
for a fresh, healthy preflight result.
"""
import http.server
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import agent


class _FakeProductionAgent(http.server.BaseHTTPRequestHandler):
    healthy = True
    build_enabled = False
    upload_called = False
    deploy_called = False

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return "session=fake" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path in ("/login", "/"):
            data = '<input name="csrf" value="FAKECSRF">'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            if self.path == "/login":
                self.send_header("Set-Cookie", "session=fake; Path=/")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._authed():
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        cls = type(self)
        if self.path == "/api/status":
            self._json(200, {
                "agent_version": "2.16.10-docker-runtime", "server_role": "PRODUCTION",
                "build_enabled": cls.build_enabled, "deploy_enabled": True,
                "job": {"status": "success"},
                "mes": {"healthy": cls.healthy, "version": "65.8.44.65",
                        "migration_head": "0029_kiosk_ota_fleet_safety",
                        "docker": {"service_health": {"postgres": "healthy", "mesflow": "healthy"}}},
            })
        elif self.path == "/api/ops/summary":
            self._json(200, {"disk": {"percent": 42.0, "free": "50.0 GB", "total": "100.0 GB"},
                              "docker_running": 3, "docker_unhealthy": 0})
        elif self.path == "/api/ops/backups":
            self._json(200, {"count": 5, "free_space": "50.0 GB",
                              "most_recent": {"name": "source_x", "modified_at": "2026-08-13 06:00:00"}})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/login":
            self.send_response(302); self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session=fake; Path=/")
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        if self.path == "/upload":
            type(self).upload_called = True
        if self.path.startswith("/deploy/"):
            type(self).deploy_called = True
        self._json(200, {"ok": True})


class ProductionPreflightTests(unittest.TestCase):
    def setUp(self):
        _FakeProductionAgent.healthy = True
        _FakeProductionAgent.build_enabled = False
        _FakeProductionAgent.upload_called = False
        _FakeProductionAgent.deploy_called = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeProductionAgent)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.old_config_dir = agent.CONFIG_DIR
        agent.CONFIG_DIR = self.config_dir

    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=5)
        agent.CONFIG_DIR = self.old_config_dir
        self.tmp.cleanup()

    def test_preflight_is_strictly_read_only_and_reports_healthy(self):
        url = f"http://127.0.0.1:{self.port}"
        with patch.dict("os.environ", {
            "MESFLOW_PRODUCTION_AGENT_URL": url,
            "MESFLOW_PRODUCTION_AGENT_USER": "admin",
            "MESFLOW_PRODUCTION_AGENT_PASSWORD": "irrelevant",
        }):
            report = agent._run_production_preflight()
        self.assertFalse(_FakeProductionAgent.upload_called, "preflight must never upload a release")
        self.assertFalse(_FakeProductionAgent.deploy_called, "preflight must never trigger a deploy")
        self.assertTrue(report["healthy"])
        self.assertEqual(report["mes"]["schema_actual"], "0029_kiosk_ota_fleet_safety")
        self.assertEqual(report["postgres"]["health"], "healthy")
        self.assertEqual(report["disk"]["percent"], 42.0)
        self.assertEqual(report["backups"]["count"], 5)
        saved = agent.load_json(self.config_dir / "production-preflight.json", {})
        self.assertEqual(saved, report)

    def test_preflight_reports_unhealthy_when_build_enabled_on_target(self):
        _FakeProductionAgent.build_enabled = True
        url = f"http://127.0.0.1:{self.port}"
        with patch.dict("os.environ", {
            "MESFLOW_PRODUCTION_AGENT_URL": url,
            "MESFLOW_PRODUCTION_AGENT_USER": "admin",
            "MESFLOW_PRODUCTION_AGENT_PASSWORD": "irrelevant",
        }):
            report = agent._run_production_preflight()
        self.assertFalse(report["healthy"], "a target with build enabled must never be reported healthy for Production")

    def test_preflight_not_configured_raises_clear_error(self):
        with patch.dict("os.environ", {}, clear=False):
            for k in ("MESFLOW_PRODUCTION_AGENT_URL", "MESFLOW_PRODUCTION_AGENT_USER", "MESFLOW_PRODUCTION_AGENT_PASSWORD"):
                import os as _os
                _os.environ.pop(k, None)
            with self.assertRaisesRegex(agent._RemoteAgentError, "PRODUCTION_AGENT_NOT_CONFIGURED"):
                agent._run_production_preflight()


class ProductionGateFreshnessTests(unittest.TestCase):
    """_release_summary()'s production gate must require a *fresh*, healthy
    preflight -- an old cached 'healthy' must not keep gating PASS forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"; self.source_dir.mkdir()
        self.old = {"MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR,
                    "CONFIG_DIR": agent.CONFIG_DIR, "PROMOTION_STATE_FILE": agent.PROMOTION_STATE_FILE,
                    "STATE_FILE": agent.STATE_FILE}
        agent.MESFLOW_SOURCE_DIR = self.source_dir
        agent.CONFIG_DIR = root
        agent.PROMOTION_STATE_FILE = root / "promotion-state.json"
        agent.STATE_FILE = root / "state.json"

    def tearDown(self):
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _write_preflight(self, checked_at, healthy=True):
        agent.save_json(agent.CONFIG_DIR / "production-preflight.json",
                         {"checked_at": checked_at, "healthy": healthy})

    def test_stale_preflight_blocks_production(self):
        stale = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_preflight(stale, healthy=True)
        summary = agent._release_summary()
        self.assertFalse(summary["production_target_healthy"])
        self.assertFalse(summary["production_preflight_fresh"])

    def test_fresh_healthy_preflight_is_recognized(self):
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_preflight(fresh, healthy=True)
        summary = agent._release_summary()
        self.assertTrue(summary["production_target_healthy"])
        self.assertTrue(summary["production_preflight_fresh"])

    def test_fresh_unhealthy_preflight_does_not_count(self):
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_preflight(fresh, healthy=False)
        summary = agent._release_summary()
        self.assertFalse(summary["production_target_healthy"])

    def test_missing_preflight_blocks_production(self):
        summary = agent._release_summary()
        self.assertFalse(summary["production_target_healthy"])
        self.assertIsNotNone(summary["blocked"]["production"])
        self.assertIn("target", summary["blocked"]["production"].lower())


if __name__ == "__main__":
    unittest.main()
