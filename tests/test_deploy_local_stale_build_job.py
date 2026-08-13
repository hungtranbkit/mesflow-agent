"""Regression test for a real bug found live during Production Test
promotion verification: after the immutable-release guard correctly
refused to rebuild an already-released version (release_build_job ends up
FAILED with message VERSION_ALREADY_RELEASED), Deploy Local for that exact,
still-perfectly-valid, already-built release was blocked with
LOCAL_BUILD_REQUIRED -- because the endpoint gated on "was the most recent
build job SUCCESS" instead of "does a valid artifact exist on disk".
"""
import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import agent


class DeployLocalStaleBuildJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"
        self.source_dir.mkdir()
        self.releases_root = root / "artifacts" / "releases"
        self.version = "65.8.44.69"
        release = self.releases_root / self.version
        release.mkdir(parents=True)

        # Build a real, valid image-release package on disk, exactly what
        # build-release.sh produces and what validate_image_release_contract
        # requires.
        pkg_root = root / "pkgstage" / "mesflow-release"
        pkg_root.mkdir(parents=True)
        (pkg_root / "VERSION.txt").write_text(self.version)
        (pkg_root / "compose.yml").write_text("services: {}\n")
        release_json = {
            "type": "mesflow-image-release", "version": self.version,
            "image": f"mesflow-app:{self.version}",
            "image_digest": "sha256:" + "c" * 64, "image_id": "sha256:" + "c" * 64,
            "source_commit": "deadbeef", "schema_revision": "0029_kiosk_ota_fleet_safety",
        }
        (pkg_root / "release.json").write_text(json.dumps(release_json))
        (pkg_root / "PROMOTION.json").write_text(json.dumps({
            "version": self.version, "local": {"status": "NOT_DEPLOYED"},
            "production_test": {"status": "NOT_DEPLOYED"}, "production": {"status": "NOT_DEPLOYED"},
        }))
        checksums = pkg_root / "checksums.txt"
        checksums.write_text("dummy  VERSION.txt\n")
        zip_path = release / f"MESFlow_{self.version}.deploy.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for f in pkg_root.iterdir():
                z.write(f, f"mesflow-release/{f.name}")
        (release / "release.json").write_text(json.dumps(release_json))
        (release / "image-info.json").write_text(json.dumps({
            "image": release_json["image"], "digest": release_json["image_digest"],
            "source_commit": release_json["source_commit"], "version": self.version,
        }))

        self.old = {
            "MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR,
            "STATE_FILE": agent.STATE_FILE,
            "RELEASES_DIR": agent.RELEASES_DIR,
            "UPLOAD_DIR": agent.UPLOAD_DIR,
        }
        agent.MESFLOW_SOURCE_DIR = self.source_dir
        agent.STATE_FILE = root / "state.json"
        agent.RELEASES_DIR = root / "agent_releases"; agent.RELEASES_DIR.mkdir()
        agent.UPLOAD_DIR = root / "uploads"; agent.UPLOAD_DIR.mkdir()

    def tearDown(self):
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _login(self, client):
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "test-csrf"

    def test_deploy_local_not_blocked_by_stale_failed_build_job(self):
        # Simulate exactly the observed real-world state: the last build
        # attempt was correctly rejected (duplicate version), so
        # release_build_job.status == FAILED, but its `latest` field still
        # carries the still-valid prior release (that's how the real Agent
        # behaves -- release_build_update merges fields, never clearing
        # `latest` on failure).
        agent.save_json(agent.STATE_FILE, {
            "release_build_job": {
                "status": "FAILED", "message": "BUILD_FAILED",
                "error_code": "VERSION_ALREADY_RELEASED",
                "latest": {"version": self.version},
            }
        })
        with patch.object(agent, "_release_contamination", return_value={"contaminated": False}), \
             patch.object(agent, "start_deploy") as start_deploy:
            client = agent.app.test_client()
            self._login(client)
            resp = client.post("/api/release-manager/deploy-local")
        self.assertEqual(resp.status_code, 202, resp.get_json())
        self.assertTrue(resp.get_json().get("ok"))
        start_deploy.assert_called_once_with(self.version)

    def test_deploy_local_still_blocked_when_no_artifact_exists(self):
        agent.save_json(agent.STATE_FILE, {"release_build_job": {"status": "FAILED"}})
        with patch.object(agent, "_release_summary", return_value={"latest": {}}):
            client = agent.app.test_client()
            self._login(client)
            resp = client.post("/api/release-manager/deploy-local")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json().get("error"), "LOCAL_BUILD_REQUIRED")


if __name__ == "__main__":
    unittest.main()
