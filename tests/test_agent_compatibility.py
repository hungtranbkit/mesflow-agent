"""Agent version compatibility classification (_agent_compatibility) and the
TARGET_AGENT_INCOMPATIBLE gate on Promote Production Test. Never blocks
solely because AGENT_VERSION strings differ -- only a genuinely missing
required capability, or an unreachable/misconfigured target, blocks.
"""
import hashlib
import http.server
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import agent


class AgentCompatibilityClassificationTests(unittest.TestCase):
    def test_same_version_is_compatible(self):
        status = {"agent_version": agent.AGENT_VERSION, "api_protocol_version": agent.API_PROTOCOL_VERSION,
                   "capabilities": list(agent.AGENT_CAPABILITIES)}
        result = agent._agent_compatibility(status)
        self.assertEqual(result["status"], "COMPATIBLE")

    def test_older_but_fully_capable_is_update_available_not_blocked(self):
        status = {"agent_version": "2.18.0-docker-runtime", "api_protocol_version": agent.API_PROTOCOL_VERSION,
                   "capabilities": sorted(agent.REQUIRED_PROMOTION_CAPABILITIES)}
        result = agent._agent_compatibility(status)
        self.assertEqual(result["status"], "UPDATE_AVAILABLE")

    def test_missing_capability_is_incompatible_with_named_reason(self):
        caps = sorted(agent.REQUIRED_PROMOTION_CAPABILITIES - {"image_release_receive"})
        status = {"agent_version": "2.5.0-docker-runtime", "api_protocol_version": 1, "capabilities": caps}
        result = agent._agent_compatibility(status)
        self.assertEqual(result["status"], "INCOMPATIBLE")
        self.assertIn("image_release_receive", result["reason"])
        self.assertIn("2.5.0-docker-runtime", result["reason"])

    def test_legacy_agent_without_capability_reporting_is_update_available(self):
        # Matches the real deploy.mesflow.net Agent (2.16.10) at the time
        # this feature was added -- it demonstrably works (see
        # reports/PRODUCTION_TEST_PROMOTION_65_8_44_69.md) but predates
        # capability reporting entirely.
        status = {"agent_version": "2.16.10-docker-runtime"}
        result = agent._agent_compatibility(status)
        self.assertEqual(result["status"], "UPDATE_AVAILABLE")

    def test_unreachable_target_is_offline(self):
        result = agent._agent_compatibility({"_unreachable": True, "error": "Connection refused"})
        self.assertEqual(result["status"], "OFFLINE")


class _FakeAgentWithCapabilities(http.server.BaseHTTPRequestHandler):
    capabilities = list(agent.AGENT_CAPABILITIES)
    role = "PRODUCTION_TEST"
    build_enabled = False
    deploy_called = False

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        if not isinstance(body, str):
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 200 and self.path == "/login":
            self.send_header("Set-Cookie", "session=fake; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/login", "/"):
            self._send(200, '<input name="csrf" value="FAKECSRF">')
            return
        if "session=fake" not in (self.headers.get("Cookie") or ""):
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        if self.path == "/api/status":
            cls = type(self)
            self._send(200, {"agent_version": "9.9.9-docker-runtime", "api_protocol_version": 2,
                              "capabilities": cls.capabilities, "server_role": cls.role,
                              "build_enabled": cls.build_enabled, "deploy_enabled": True,
                              "job": {"status": "success", "version": "1.2.3.4"}, "mes": {"healthy": True, "version": "1.2.3.4"}})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/login":
            self.send_response(302); self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session=fake; Path=/")
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        if self.path == "/upload":
            type(self).deploy_called = False  # upload alone doesn't count
        if self.path.startswith("/deploy/"):
            type(self).deploy_called = True
        self._send(200, {"ok": True})


class PromoteTestIncompatibleTargetTests(unittest.TestCase):
    def setUp(self):
        _FakeAgentWithCapabilities.capabilities = list(agent.AGENT_CAPABILITIES)
        _FakeAgentWithCapabilities.role = "PRODUCTION_TEST"
        _FakeAgentWithCapabilities.build_enabled = False
        _FakeAgentWithCapabilities.deploy_called = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeAgentWithCapabilities)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"; self.source_dir.mkdir()
        self.releases_dir = root / "artifacts" / "releases"
        self.version = "1.2.3.4"
        release = self.releases_dir / self.version; release.mkdir(parents=True)
        zip_path = release / f"MESFlow_{self.version}.deploy.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("mesflow-release/VERSION.txt", self.version)
        pkg_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        release_json = {"type": "mesflow-image-release", "version": self.version,
                         "image": f"mesflow-app:{self.version}", "image_digest": "sha256:" + "d" * 64,
                         "image_id": "sha256:" + "d" * 64, "source_commit": "x", "schema_revision": "0001_core"}
        (release / "release.json").write_text(json.dumps(release_json))
        (release / "image-info.json").write_text(json.dumps(release_json))

        self.old = {"MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR, "PROMOTION_STATE_FILE": agent.PROMOTION_STATE_FILE,
                     "STATE_FILE": agent.STATE_FILE}
        agent.MESFLOW_SOURCE_DIR = self.source_dir
        self.promo_file = root / "promotion-state.json"
        agent.PROMOTION_STATE_FILE = self.promo_file
        agent.STATE_FILE = root / "state.json"
        agent.save_json(self.promo_file, {"version": self.version, "local_pass": True, "local_status": "PASS",
                                            "package_sha256": pkg_sha, "image_id": release_json["image_id"]})

    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=5)
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _run(self):
        target_url = f"http://127.0.0.1:{self.port}"
        with patch.object(agent, "MES_VERIFY_TIMEOUT", 5), patch.dict("os.environ", {
            "MESFLOW_PRODUCTION_TEST_AGENT_URL": target_url,
            "MESFLOW_PRODUCTION_TEST_AGENT_USER": "admin",
            "MESFLOW_PRODUCTION_TEST_AGENT_PASSWORD": "irrelevant",
        }):
            agent._run_promote_test(self.version)
        return agent.load_state().get("promote_test_job") or {}

    def test_incompatible_target_blocks_promotion(self):
        _FakeAgentWithCapabilities.capabilities = sorted(set(agent.AGENT_CAPABILITIES) - {"image_release_receive"})
        job = self._run()
        self.assertEqual(job.get("status"), "FAILED")
        self.assertIn("TARGET_AGENT_INCOMPATIBLE", job.get("message", ""))
        self.assertIn("image_release_receive", job.get("message", ""))
        self.assertFalse(_FakeAgentWithCapabilities.deploy_called, "an incompatible target must never receive a deploy call")

    def test_wrong_role_blocks_promotion(self):
        _FakeAgentWithCapabilities.role = "DEV"
        job = self._run()
        self.assertEqual(job.get("status"), "FAILED")
        self.assertIn("TARGET_AGENT_WRONG_ROLE", job.get("message", ""))
        self.assertFalse(_FakeAgentWithCapabilities.deploy_called)

    def test_build_enabled_target_blocks_promotion(self):
        _FakeAgentWithCapabilities.build_enabled = True
        job = self._run()
        self.assertEqual(job.get("status"), "FAILED")
        self.assertIn("TARGET_AGENT_BUILD_ENABLED", job.get("message", ""))
        self.assertFalse(_FakeAgentWithCapabilities.deploy_called)

    def test_fully_compatible_target_is_allowed_through_the_gate(self):
        # Compatible + correct role + build disabled must pass the gate
        # (the fake server then answers success for upload/deploy/status).
        job = self._run()
        self.assertNotIn("TARGET_AGENT", job.get("message", ""))
        self.assertNotEqual(job.get("status"), "FAILED")


if __name__ == "__main__":
    unittest.main()
