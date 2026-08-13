"""Verifies the real Promote Production Test mechanics (login, CSRF, upload,
deploy trigger, polling, TEST_PASS recording) against a lightweight fake
target Agent HTTP server -- real HTTP calls over a real socket, exercising
the exact client code the DEV Agent uses against a real remote Agent,
without requiring a full second MESFlow+PostgreSQL stack.
"""
import hashlib
import http.server
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import agent


class _FakeTargetAgent(http.server.BaseHTTPRequestHandler):
    """Implements just enough of the real Agent's HTTP contract to exercise
    _remote_agent_client / _remote_agent_upload / _remote_agent_deploy."""

    version = "9.9.9.9"
    received_sha256 = None
    deploy_called = False

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 200 and self.path == "/login" and self.command == "GET":
            self.send_header("Set-Cookie", "session=fake; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/login", "/"):
            self._send(200, '<input name="csrf" value="FAKECSRF123">')
        elif self.path == "/api/status":
            # Mirrors the real Agent: /api/status is not login-exempt. An
            # unauthenticated request must NOT get the JSON success payload
            # -- it must be redirected to /login instead, exactly like the
            # real Agent's @app.before_request protect(). This is what
            # exposed the real bug: _remote_agent_status() used to build a
            # bare, cookie-less request.
            if "session=fake" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, json.dumps({
                "job": {"status": "success", "version": type(self).version},
                "mes": {"healthy": True, "version": type(self).version,
                        "health_payload": {"schema_version": "x"}},
            }), "application/json")
        else:
            self._send(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/login":
            data = b""
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session=fake; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/upload":
            # Extract the uploaded file's bytes from the multipart body and
            # hash them, the same way a real upload endpoint would verify.
            type(self).received_sha256 = hashlib.sha256(body).hexdigest()  # coarse but proves bytes arrived
            self._send(200, json.dumps({"ok": True}), "application/json")
        elif self.path.startswith("/deploy/"):
            type(self).deploy_called = True
            self._send(200, json.dumps({"ok": True}), "application/json")
        else:
            self._send(404, "not found")


class PromoteProductionTestFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"
        self.source_dir.mkdir()
        self.releases_dir = root / "artifacts" / "releases"
        version = "9.9.9.9"
        release = self.releases_dir / version
        release.mkdir(parents=True)
        zip_path = release / f"MESFlow_{version}.deploy.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr(f"mesflow-release/VERSION.txt", version)
        pkg_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        release_json = {
            "type": "mesflow-image-release", "version": version,
            "image": f"mesflow-app:{version}",
            "image_digest": "sha256:" + "a" * 64, "image_id": "sha256:" + "a" * 64,
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
            "package_sha256": pkg_sha, "image_id": release_json["image_id"],
        })
        self.version = version
        self.zip_sha = pkg_sha

        _FakeTargetAgent.version = version
        _FakeTargetAgent.received_sha256 = None
        _FakeTargetAgent.deploy_called = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeTargetAgent)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def test_release_summary_shows_live_promote_test_job_state(self):
        # Real bug found live: the Production Test pipeline indicator sat
        # at NOT_DEPLOYED through the entire upload+deploy (which visibly
        # succeeded on the real remote target), only ever jumping straight
        # to PASS/FAILED at the very end -- because _release_summary() read
        # only the *persisted* promotion-state.json test_status, never the
        # actively-running promote_test_job.
        agent.save_json(agent.STATE_FILE, {
            "promote_test_job": {"status": "DEPLOYING", "version": self.version},
        })
        summary = agent._release_summary()
        self.assertEqual(summary["states"]["test"], "DEPLOYING")

    def test_release_summary_ignores_live_job_for_a_different_version(self):
        agent.save_json(agent.STATE_FILE, {
            "promote_test_job": {"status": "DEPLOYING", "version": "1.2.3.4"},
        })
        summary = agent._release_summary()
        self.assertNotEqual(summary["states"]["test"], "DEPLOYING")

    def test_promote_test_flow_logs_in_uploads_deploys_and_records_pass(self):
        target_url = f"http://127.0.0.1:{self.port}"
        with patch.object(agent, "MES_VERIFY_TIMEOUT", 5), \
             patch.dict("os.environ", {
                 "MESFLOW_PRODUCTION_TEST_AGENT_URL": target_url,
                 "MESFLOW_PRODUCTION_TEST_AGENT_USER": "admin",
                 "MESFLOW_PRODUCTION_TEST_AGENT_PASSWORD": "irrelevant-fake-password",
             }):
            agent._run_promote_test(self.version)

        self.assertTrue(_FakeTargetAgent.deploy_called, "deploy endpoint was never called")
        self.assertIsNotNone(_FakeTargetAgent.received_sha256, "upload never reached the target")

        state = agent.load_json(self.promo_file, {})
        self.assertEqual(state.get("test_status"), "PASS")
        self.assertTrue(state.get("test_pass"))
        self.assertEqual(state.get("test_target"), target_url)

        # PROMOTION.json is mutable (unlike release.json/checksums.txt/the
        # ZIP) and external tooling (scripts/promote-test.sh) reads it
        # directly -- it must not silently drift from what the Agent itself
        # believes.
        release_promotion = agent.load_json(self.releases_dir / self.version / "PROMOTION.json", {})
        self.assertEqual(release_promotion.get("production_test", {}).get("status"), "success")

    def test_promote_test_refuses_without_local_pass(self):
        agent.save_json(self.promo_file, {"version": self.version, "local_pass": False})
        with patch.object(agent, "MES_VERIFY_TIMEOUT", 5):
            agent._run_promote_test(self.version)
        job = agent.load_state().get("promote_test_job") or {}
        self.assertEqual(job.get("status"), "FAILED")
        self.assertIn("LOCAL_PASS_REQUIRED", job.get("message", ""))
        # Must not have contacted the target at all.
        self.assertFalse(_FakeTargetAgent.deploy_called)

    def test_promote_test_refuses_contaminated_release(self):
        contaminated_marker = self.releases_dir / self.version / "CONTAMINATED.json"
        contaminated_marker.write_text(json.dumps({"contaminated": True, "reason": "test marker"}))
        with patch.object(agent, "MES_VERIFY_TIMEOUT", 5):
            agent._run_promote_test(self.version)
        job = agent.load_state().get("promote_test_job") or {}
        self.assertEqual(job.get("status"), "FAILED")
        self.assertIn("CONTAMINATED", job.get("message", ""))
        self.assertFalse(_FakeTargetAgent.deploy_called)


if __name__ == "__main__":
    unittest.main()
