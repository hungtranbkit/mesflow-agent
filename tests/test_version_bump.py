"""Version bump: scripts/bump-version.sh + the Release Manager UI's
'Nâng version (chưa build)' button. Updates VERSION.txt / __init__.py /
release.json / compose.yml consistently, source-only, never builds.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent

REAL_SCRIPT = Path(__file__).resolve().parent.parent.parent / "mesflow" / "scripts" / "bump-version.sh"


def _seed_source(root, version):
    (root / "app" / "mesflow").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "VERSION.txt").write_text(version)
    # Mirrors the real app/mesflow/__init__.py mechanism: __version__ is read
    # dynamically from VERSION.txt at import time, not a hardcoded literal
    # (see that file's own docstring). scripts/bump-version.sh's
    # sync_and_verify() only checks the _VERSION_FILE marker stays intact;
    # it never rewrites this file -- a static literal here always fails
    # that check regardless of which version was bumped to.
    (root / "app" / "mesflow" / "__init__.py").write_text(
        "import os\n"
        "_VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'VERSION.txt')\n"
        "with open(_VERSION_FILE) as _f:\n"
        "    __version__ = _f.read().strip()\n"
    )
    (root / "release.json").write_text(json.dumps({"name": "MESFlow", "version": version, "phase": "x"}, indent=2) + "\n")
    (root / "compose.yml").write_text(f"services:\n  mesflow-app:\n    image: ${{MESFLOW_IMAGE:-mesflow-app:{version}}}\n")
    shutil.copy(REAL_SCRIPT, root / "scripts" / "bump-version.sh")
    (root / "scripts" / "bump-version.sh").chmod(0o755)


class VersionBumpTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(REAL_SCRIPT.is_file(), f"real script missing: {REAL_SCRIPT}")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "mesflow"
        _seed_source(self.src, "1.0.0.1")
        (root / "artifacts" / "releases").mkdir(parents=True)

        self.old = {"MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR, "MESFLOW_BUILD_ENABLED": agent.MESFLOW_BUILD_ENABLED,
                    "STATE_FILE": agent.STATE_FILE}
        agent.MESFLOW_SOURCE_DIR = self.src
        agent.MESFLOW_BUILD_ENABLED = True
        agent.STATE_FILE = root / "state.json"

    def tearDown(self):
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _login(self, client):
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "test-csrf"

    def test_default_bump_increments_last_segment_everywhere(self):
        entry = agent._run_version_bump("")
        self.assertEqual(entry["before"], "1.0.0.1")
        self.assertEqual(entry["after"], "1.0.0.2")
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.2")
        # __init__.py is deliberately never rewritten (it reads VERSION.txt
        # dynamically); only its dynamic-read marker is required to survive.
        self.assertIn("_VERSION_FILE", (self.src / "app" / "mesflow" / "__init__.py").read_text())
        self.assertIn('"version": "1.0.0.2"', (self.src / "release.json").read_text())
        self.assertIn("mesflow-app:1.0.0.2", (self.src / "compose.yml").read_text())
        # Never touches anything build-related.
        self.assertIsNone(agent.load_state().get("release_build_job"))

    def test_explicit_target_version(self):
        entry = agent._run_version_bump("1.0.0.9")
        self.assertEqual(entry["after"], "1.0.0.9")
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.9")

    def test_rejects_non_newer_version(self):
        with self.assertRaises(RuntimeError):
            agent._run_version_bump("1.0.0.1")
        with self.assertRaises(RuntimeError):
            agent._run_version_bump("0.9.9.9")
        # Nothing was touched by the rejected attempts.
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.1")

    def test_rejects_already_released_target(self):
        frozen = self.src.parent / "artifacts" / "releases" / "1.0.0.5"
        frozen.mkdir(parents=True)
        (frozen / "release.json").write_text("{}")
        with self.assertRaises(RuntimeError):
            agent._run_version_bump("1.0.0.5")
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.1")

    def test_invalid_target_format_rejected_before_running_script(self):
        with self.assertRaises(ValueError):
            agent._run_version_bump("not-a-version")

    def test_disabled_when_build_not_enabled(self):
        agent.MESFLOW_BUILD_ENABLED = False
        with self.assertRaises(ValueError):
            agent._run_version_bump("")
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.1")

    def test_endpoint_bumps_and_reports_result(self):
        with patch.object(agent, "_release_summary", return_value={"source_version": "1.0.0.1"}):
            client = agent.app.test_client()
            self._login(client)
            resp = client.post("/api/release-manager/bump-version", json={}, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["before"], "1.0.0.1")
        self.assertEqual(data["after"], "1.0.0.2")

    def test_endpoint_rejects_backward_version_with_409(self):
        client = agent.app.test_client()
        self._login(client)
        resp = client.post("/api/release-manager/bump-version", json={"target_version": "0.0.0.1"}, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.get_json()["ok"])

    def test_endpoint_requires_auth(self):
        # The global before_request hook redirects unauthenticated requests
        # to /login before the route body's own defensive 401 check ever
        # runs -- matches every sibling /api/release-manager/* endpoint.
        client = agent.app.test_client()
        resp = client.post("/api/release-manager/bump-version", json={}, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))
        self.assertEqual((self.src / "VERSION.txt").read_text().strip(), "1.0.0.1")

    def test_status_endpoint_reports_last_bump(self):
        agent._run_version_bump("")
        client = agent.app.test_client()
        self._login(client)
        resp = client.get("/api/release-manager/bump-version")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["last"]["before"], "1.0.0.1")
        self.assertEqual(data["last"]["after"], "1.0.0.2")

    def test_release_workspace_renders_bump_version_button(self):
        with patch.object(agent, "_release_contamination", return_value={"contaminated": False}):
            client = agent.app.test_client()
            self._login(client)
            resp = client.get("/releases")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("bumpVersionButton", body)
        self.assertIn("/api/release-manager/bump-version", body)


if __name__ == "__main__":
    unittest.main()
