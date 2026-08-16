"""Verifies the "release-test" QA evidence gate for v70+ releases.

Real incident this covers: Promote Production Test deployed and verified
the image correctly, but TEST never left AWAITING_QA -- nothing in the
codebase ever invoked a "release-test" QA run (grep confirmed
_invoke_qa_profile was only ever called with "LOCAL"). The fix: the
promoting (DEV) Agent asks the TARGET Agent -- the only one that can reach
that MESFlow instance's own internal-only QA Center -- to run
"release-test" via POST /release-manager/run-test-gate/<version>, and
records the returned evidence into its own promotion state. See
_run_promote_test()'s v70 branch, _run_qa_profile(),
_remote_agent_run_test_gate().

Same lightweight fake-target-over-a-real-socket technique as
test_promote_production_test.py -- exercises the real HTTP client code
without a second full Agent process.
"""
import http.server
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import agent


class _FakeTestAgentWithGate(http.server.BaseHTTPRequestHandler):
    """Adds a working /release-manager/run-test-gate/<version> to the same
    minimal contract test_promote_production_test.py's fake target uses."""

    version = "70.0.0.1"
    artifact_digest = "sha256:" + "a" * 64
    gate_response_status = "PASSED"
    gate_called_with = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 200 and self.path in ("/login", "/") and self.command == "GET":
            self.send_header("Set-Cookie", "session=fake; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/login", "/"):
            self._send(200, '<input name="csrf" value="FAKECSRF123">', "text/html")
        elif self.path == "/api/status":
            if "session=fake" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, json.dumps({
                "server_role": "PRODUCTION_TEST", "build_enabled": False,
                "agent_version": "2.24.4-docker-runtime", "api_protocol_version": 2,
                "capabilities": ["image_release_receive", "digest_verify", "remote_deploy",
                                  "deploy_status", "health_status", "mes_release_test_gate"],
                "job": {"status": "success", "version": type(self).version},
                "mes": {"healthy": True, "version": type(self).version,
                        "health_payload": {"schema_version": "x"}},
            }))
        else:
            self._send(404, '{"ok":false}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/login":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session=fake; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/upload":
            self._send(200, json.dumps({"ok": True}))
        elif self.path.startswith("/deploy/"):
            self._send(200, json.dumps({"ok": True}))
        elif self.path.startswith("/release-manager/run-test-gate/"):
            type(self).gate_called_with = self.path.rsplit("/", 1)[-1]
            payload = {
                "run_id": "TEST-fake-run-1", "environment": "TEST", "profile": "release-test",
                "artifact_digest": type(self).artifact_digest,
                "status": type(self).gate_response_status,
                "total": 3, "passed": 3 if type(self).gate_response_status == "PASSED" else 2,
                "failed": 0 if type(self).gate_response_status == "PASSED" else 1,
                "ended_at": "2026-01-01T00:00:00",
            }
            self._send(200, json.dumps({"ok": True, "payload": payload}))
        else:
            self._send(404, '{"ok":false}')


class ReleaseTestGateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"
        self.source_dir.mkdir()
        self.releases_dir = root / "artifacts" / "releases"
        version = "70.0.0.1"  # v70+ on purpose -- exercises the QA-gate branch
        release = self.releases_dir / version
        release.mkdir(parents=True)
        zip_path = release / f"MESFlow_{version}.deploy.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("mesflow-release/VERSION.txt", version)
        pkg_sha = __import__("hashlib").sha256(zip_path.read_bytes()).hexdigest()
        digest = "sha256:" + "a" * 64
        release_json = {
            "type": "mesflow-image-release", "version": version,
            "image": f"mesflow-app:{version}", "image_digest": digest, "image_id": digest,
            "source_commit": "deadbeef", "schema_revision": "0001_core",
        }
        (release / "release.json").write_text(json.dumps(release_json))
        (release / "image-info.json").write_text(json.dumps(release_json))

        self.old = {
            "MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR,
            "PROMOTION_STATE_FILE": agent.PROMOTION_STATE_FILE,
            "STATE_FILE": agent.STATE_FILE,
        }
        agent.MESFLOW_SOURCE_DIR = self.source_dir
        self.promo_file = root / "promotion-state.json"
        agent.PROMOTION_STATE_FILE = self.promo_file
        agent.STATE_FILE = root / "state.json"
        agent.save_json(self.promo_file, {
            "version": version, "local_pass": True, "local_status": "PASS",
            "package_sha256": pkg_sha, "image_id": digest,
        })
        self.version = version

        _FakeTestAgentWithGate.version = version
        _FakeTestAgentWithGate.artifact_digest = digest
        _FakeTestAgentWithGate.gate_response_status = "PASSED"
        _FakeTestAgentWithGate.gate_called_with = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeTestAgentWithGate)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _run(self):
        target_url = f"http://127.0.0.1:{self.port}"
        with patch.object(agent, "MES_VERIFY_TIMEOUT", 5), \
             patch.dict("os.environ", {
                 "MESFLOW_PRODUCTION_TEST_AGENT_URL": target_url,
                 "MESFLOW_PRODUCTION_TEST_AGENT_USER": "admin",
                 "MESFLOW_PRODUCTION_TEST_AGENT_PASSWORD": "irrelevant-fake-password",
             }):
            agent._run_promote_test(self.version)

    def test_v70_promote_calls_remote_gate_and_records_test_status(self):
        self._run()
        self.assertEqual(_FakeTestAgentWithGate.gate_called_with, self.version,
                          "the target's release-test gate endpoint was never called")
        state = agent.load_json(self.promo_file, {})
        # The TEST run's own outcome is PASS...
        self.assertEqual(state.get("test_status"), "PASS")
        # ...but test_pass is the FULL holistic gate (LOCAL run + TEST
        # deployment + TEST run + no blocking issues), which this test
        # never stages a matching LOCAL run for -- so it must legitimately
        # stay False, never silently upgraded to True just because the
        # remote check passed. Never claim PASS without full evidence.
        self.assertIn("test_pass", state)
        self.assertFalse(state.get("test_pass"))
        # Some non-READY decision was computed (this test never stages a
        # matching LOCAL run, so it can't legitimately be
        # READY_FOR_PRODUCTION_APPROVAL) -- the exact code isn't the point
        # here, only that _record_gate_run() actually ran the full policy.
        self.assertTrue(state.get("release_gate"))
        self.assertNotEqual(state.get("release_gate"), "READY_FOR_PRODUCTION_APPROVAL")

    def test_v70_promote_records_failed_qa_check_without_masking_deploy_success(self):
        _FakeTestAgentWithGate.gate_response_status = "FAILED"
        self._run()
        state = agent.load_json(self.promo_file, {})
        self.assertEqual(state.get("test_status"), "FAIL")
        self.assertFalse(state.get("test_pass"))
        # Deployment itself still succeeded -- test_deployed_digest proves
        # the real deploy+verify steps ran and were not skipped/masked by
        # the QA gate outcome.
        self.assertEqual(state.get("test_deployed_digest"), _FakeTestAgentWithGate.artifact_digest)
        job = agent.load_state().get("promote_test_job") or {}
        self.assertEqual(job.get("status"), "SUCCESS")

    def test_target_without_gate_capability_degrades_visibly(self):
        # Simulate an older target Agent, pre-dating this feature: no
        # mes_release_test_gate in its reported capabilities.
        original_get_json = agent._remote_agent_get_json

        def patched(opener, base, path, timeout=25):
            result = original_get_json(opener, base, path, timeout)
            if path == "/api/status" and isinstance(result, dict):
                result = dict(result)
                result["capabilities"] = [c for c in result.get("capabilities", []) if c != "mes_release_test_gate"]
            return result

        with patch.object(agent, "_remote_agent_get_json", side_effect=patched):
            self._run()
        self.assertIsNone(_FakeTestAgentWithGate.gate_called_with,
                           "must not call the gate endpoint on a target that never advertised support for it")
        state = agent.load_json(self.promo_file, {})
        self.assertEqual(state.get("test_status"), "AWAITING_QA")
        job = agent.load_state().get("promote_test_job") or {}
        self.assertIn("does not support mes_release_test_gate", job.get("message", ""))


if __name__ == "__main__":
    unittest.main()
