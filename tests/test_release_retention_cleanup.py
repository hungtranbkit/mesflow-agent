"""Release retention / cleanup policy (Task 6, Part A):

Keep only MESFLOW_RELEASE_RETENTION (default 3) deployable application
releases -- current/previous/previous-2. Older releases may be cleaned only
if they are not currently deployed, an active rollback target, part of an
active deployment, being promoted, or referenced by an active job. Cleanup
removes the release ZIP, the local staging copy, and the corresponding
Docker image -- never a broad `docker system prune`, never release.json /
checksums.txt (the immutable-build guard depends on those existing
forever), and never anything under database backups.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


def _write_release(root, version, mtime_offset=0):
    release = root / version
    release.mkdir(parents=True)
    release_json = {
        "type": "mesflow-image-release", "version": version,
        "image": f"mesflow-app:{version}", "image_digest": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64, "source_commit": "deadbeef",
        "schema_revision": "head",
    }
    (release / "release.json").write_text(json.dumps(release_json))
    (release / "checksums.txt").write_text("dummy\n")
    (release / "image-info.json").write_text(json.dumps({"version": version}))
    zip_path = release / f"MESFlow_{version}.deploy.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal empty-zip EOCD, size is all that matters here
    (zip_path.parent / f"{zip_path.name}.sha256").write_text("dummy\n")
    if mtime_offset:
        t = time.time() + mtime_offset
        for f in release.iterdir():
            import os
            os.utime(f, (t, t))
        import os
        os.utime(release, (t, t))
    return release


class ReleaseRetentionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_dir = root / "mesflow"; self.source_dir.mkdir()
        self.releases_root = root / "artifacts" / "releases"; self.releases_root.mkdir(parents=True)
        self.versions = ["1.0.0.1", "1.0.0.2", "1.0.0.3", "1.0.0.4", "1.0.0.5"]
        # Older offset for earlier versions so sort-by-build-time is deterministic.
        for i, v in enumerate(self.versions):
            _write_release(self.releases_root, v, mtime_offset=i - len(self.versions))

        self.old = {
            "MESFLOW_SOURCE_DIR": agent.MESFLOW_SOURCE_DIR,
            "STATE_FILE": agent.STATE_FILE,
            "RELEASES_DIR": agent.RELEASES_DIR,
            "UPLOAD_DIR": agent.UPLOAD_DIR,
            "PROMOTION_STATE_FILE": agent.PROMOTION_STATE_FILE,
            "MESFLOW_RELEASE_RETENTION": agent.MESFLOW_RELEASE_RETENTION,
            "TARGET_HOME": agent.TARGET_HOME,
        }
        agent.MESFLOW_SOURCE_DIR = self.source_dir
        agent.STATE_FILE = root / "state.json"
        agent.RELEASES_DIR = root / "agent_releases"; agent.RELEASES_DIR.mkdir()
        for v in self.versions:
            (agent.RELEASES_DIR / v).mkdir()
            (agent.RELEASES_DIR / v / "compose.yml").write_text("services: {}\n")
        agent.UPLOAD_DIR = root / "uploads"; agent.UPLOAD_DIR.mkdir()
        agent.PROMOTION_STATE_FILE = root / "promotion-state.json"
        agent.TARGET_HOME = root / "opt-mesflow"; agent.TARGET_HOME.mkdir()
        agent.MESFLOW_RELEASE_RETENTION = 3

    def tearDown(self):
        for k, v in self.old.items():
            setattr(agent, k, v)
        self.tmp.cleanup()

    def _docker_ok(self, cmd, timeout=30, shell=False):
        # Real _run_capture() contract: {"ok","exit_code","stdout","stderr"}
        # -- not "returncode". Mocking the exact real shape here is what
        # caught a genuine bug live (the cleanup code originally checked a
        # key that doesn't exist, so every successful `docker rmi` was
        # misreported as an error).
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    def test_keeps_current_previous_previous2_and_removes_the_rest(self):
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok) as run_capture, \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertEqual(sorted(report["kept"]), ["1.0.0.3", "1.0.0.4", "1.0.0.5"])
        removed_versions = {item["version"] for item in report["removed"] if "version" in item}
        self.assertEqual(removed_versions, {"1.0.0.1", "1.0.0.2"})
        # A successful `docker rmi` (real contract: ok=True) must be recorded
        # as removed, not silently miscounted as an error.
        self.assertEqual(report["errors"], [])
        image_removals = {item["image"] for item in report["removed"] if item["type"] == "docker_image"}
        self.assertEqual(image_removals, {"mesflow-app:1.0.0.1", "mesflow-app:1.0.0.2"})
        # The release ZIP and local staging copy for cleaned versions are gone...
        self.assertFalse((self.releases_root / "1.0.0.1" / "MESFlow_1.0.0.1.deploy.zip").exists())
        self.assertFalse((agent.RELEASES_DIR / "1.0.0.1").exists())
        # ...but release.json/checksums.txt/image-info.json survive forever
        # (the immutable-once build guard depends on them).
        self.assertTrue((self.releases_root / "1.0.0.1" / "release.json").is_file())
        self.assertTrue((self.releases_root / "1.0.0.1" / "checksums.txt").is_file())
        self.assertTrue((self.releases_root / "1.0.0.1" / "image-info.json").is_file())
        # Retained versions are fully untouched.
        for v in ("1.0.0.3", "1.0.0.4", "1.0.0.5"):
            self.assertTrue((self.releases_root / v / f"MESFlow_{v}.deploy.zip").exists())
            self.assertTrue((agent.RELEASES_DIR / v).exists())
        # Only targeted `docker rmi <exact tag>` calls -- never a broad prune.
        for call in run_capture.call_args_list:
            cmd = call.args[0]
            self.assertEqual(cmd[:2], ["docker", "rmi"])
            self.assertNotIn("prune", " ".join(cmd))
        rmi_tags = {call.args[0][2] for call in run_capture.call_args_list}
        self.assertEqual(rmi_tags, {"mesflow-app:1.0.0.1", "mesflow-app:1.0.0.2"})

    def test_configurable_retention_via_env_constant(self):
        agent.MESFLOW_RELEASE_RETENTION = 1
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertEqual(report["kept"], ["1.0.0.5"])
        removed_versions = {item["version"] for item in report["removed"] if "version" in item}
        self.assertEqual(removed_versions, {"1.0.0.1", "1.0.0.2", "1.0.0.3", "1.0.0.4"})

    def test_never_cleans_currently_deployed_release_even_if_old(self):
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="1.0.0.1"):
            report = agent._cleanup_old_releases("TEST")
        self.assertIn("1.0.0.1", report["blocked"])
        self.assertIn("CURRENTLY_DEPLOYED", report["blocked"]["1.0.0.1"])
        self.assertTrue((self.releases_root / "1.0.0.1" / "MESFlow_1.0.0.1.deploy.zip").exists())

    def test_never_cleans_active_rollback_target_of_running_job(self):
        agent.save_json(agent.STATE_FILE, {
            "job": {"status": "deploying", "version": "1.0.0.5", "from_version": "1.0.0.1"},
        })
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertIn("1.0.0.1", report["blocked"])
        self.assertIn("ACTIVE_ROLLBACK_TARGET", report["blocked"]["1.0.0.1"])

    def test_never_cleans_rollback_target_of_last_completed_deploy(self):
        agent.save_json(agent.STATE_FILE, {
            "history": [{"status": "success", "version": "1.0.0.5", "from_version": "1.0.0.2"}],
        })
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertIn("1.0.0.2", report["blocked"])
        self.assertIn("ROLLBACK_TARGET_OF_LAST_DEPLOY", report["blocked"]["1.0.0.2"])

    def test_never_cleans_release_being_promoted(self):
        agent.save_json(agent.STATE_FILE, {
            "promote_test_job": {"status": "DEPLOYING", "version": "1.0.0.1"},
        })
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertIn("1.0.0.1", report["blocked"])
        self.assertIn("BEING_PROMOTED", report["blocked"]["1.0.0.1"])

    def test_never_cleans_release_still_mid_flight_through_promotion_pipeline(self):
        agent.save_json(agent.PROMOTION_STATE_FILE, {
            "version": "1.0.0.1", "local_pass": True, "test_pass": False,
        })
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertIn("1.0.0.1", report["blocked"])
        self.assertIn("PROMOTION_IN_PROGRESS", report["blocked"]["1.0.0.1"])

    def test_never_calls_docker_system_prune(self):
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok) as run_capture, \
             patch.object(agent, "current_file_version", return_value="none"):
            agent._cleanup_old_releases("TEST")
        for call in run_capture.call_args_list:
            self.assertNotEqual(call.args[0][:2], ["docker", "system"])

    def test_docker_rmi_failure_is_logged_not_fatal(self):
        def failing(cmd, timeout=30, shell=False):
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Error: image is being used"}
        with patch.object(agent, "_run_capture", side_effect=failing), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("TEST")
        self.assertTrue(report["errors"])
        # The ZIP/staging removal still proceeded even though `docker rmi` failed.
        self.assertFalse((self.releases_root / "1.0.0.1" / "MESFlow_1.0.0.1.deploy.zip").exists())

    def test_orphaned_staging_with_no_build_record_is_cleaned_but_protects_active_version(self):
        # A local staging copy whose artifacts/releases build record is
        # already gone entirely (predates this policy, or was cleaned by
        # something else) -- real state observed live on the DEV Agent
        # (RELEASES_DIR had 65.8.44.24/.26/.27/.30/.33/.37/.48/.62 with no
        # matching artifacts/releases/<version> directory at all).
        orphan = agent.RELEASES_DIR / "0.9.0.1"; orphan.mkdir()
        (orphan / "compose.yml").write_text("services: {}\n")
        protected_orphan = agent.RELEASES_DIR / "0.9.0.2"; protected_orphan.mkdir()
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="0.9.0.2"):
            report = agent._cleanup_old_releases("TEST")
        self.assertFalse(orphan.exists())
        self.assertTrue(protected_orphan.exists())
        self.assertIn("0.9.0.2", report["blocked"])
        self.assertIn("CURRENTLY_DEPLOYED", report["blocked"]["0.9.0.2"])
        removed_orphan = [item for item in report["removed"] if item.get("version") == "0.9.0.1"]
        self.assertTrue(any(item["type"] == "old_staging" for item in removed_orphan))

    def test_stray_old_temp_upload_is_removed_but_recent_one_is_kept(self):
        old_upload = agent.UPLOAD_DIR / "local_release_deadbeef"
        old_upload.mkdir(); (old_upload / "x.txt").write_text("x")
        import os
        old_time = time.time() - 7200
        os.utime(old_upload, (old_time, old_time)); os.utime(old_upload / "x.txt", (old_time, old_time))
        fresh_upload = agent.UPLOAD_DIR / "local_release_fresh"
        fresh_upload.mkdir()
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            agent._cleanup_old_releases("TEST")
        self.assertFalse(old_upload.exists())
        self.assertTrue(fresh_upload.exists())

    def test_cleanup_report_is_persisted_and_readable_via_state(self):
        with patch.object(agent, "_run_capture", side_effect=self._docker_ok), \
             patch.object(agent, "current_file_version", return_value="none"):
            report = agent._cleanup_old_releases("LOCAL_PASS")
        stored = agent.load_state().get("release_cleanup_report")
        self.assertEqual(stored["trigger"], "LOCAL_PASS")
        self.assertEqual(stored["removed"], report["removed"])

    def test_local_pass_triggers_cleanup_but_failed_local_deploy_does_not(self):
        artifact = {"version": "1.0.0.5", "image_id": "sha256:" + "a" * 64, "schema_revision": "head"}
        with patch.object(agent, "_artifact_for_release", return_value=artifact), \
             patch.object(agent, "_release_contamination", return_value={"contaminated": False}), \
             patch.object(agent, "_actual_running_mes_image_id", return_value="sha256:" + "a" * 64), \
             patch.object(agent, "_actual_schema_revision", return_value="head"), \
             patch.object(agent, "_http_smoke_ok", return_value=True), \
             patch.object(agent, "_cleanup_old_releases") as cleanup:
            agent._record_local_deploy_result("1.0.0.5", True, "")
        cleanup.assert_called_once_with("LOCAL_PASS")

        with patch.object(agent, "_artifact_for_release", return_value=artifact), \
             patch.object(agent, "_cleanup_old_releases") as cleanup:
            agent._record_local_deploy_result("1.0.0.5", False, "deploy failed")
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
