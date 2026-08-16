"""Host-side Agent updater (updater/updater.py): artifact verification,
the update/rollback state machine, and the invariant that it never issues
a command touching anything other than the mesflow-deploy-agent service
(no mesflow-app/postgres/qa-center/nginx, no `down`, no volume removal).

Imported directly from the workspace file via importlib -- this script is
intentionally NOT part of the deploy-agent package (it must keep working
even if agent.py's own dependencies are broken).
"""
import fcntl
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

UPDATER_PATH = Path(__file__).resolve().parents[1] / "updater" / "updater.py"


def load_updater(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("mesflow_updater_test", UPDATER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.UPDATER_HOME = tmp_path / "updater_home"
    module.UPDATER_HOME.mkdir()
    module.STATE_DIR = module.UPDATER_HOME / "state"; module.STATE_DIR.mkdir()
    module.LOG_FILE = module.STATE_DIR / "updater.log"
    module.TARGET_DIR = tmp_path / "target"; module.TARGET_DIR.mkdir()
    module.COMPOSE_FILES = ["compose.yml"]
    module.ENV_FILE = module.TARGET_DIR / ".env"
    (module.TARGET_DIR / "compose.yml").write_text("services: {mesflow-deploy-agent: {}}\n")
    return module


def build_artifact(tmp_path: Path, version="9.9.9-docker-runtime", image_id="sha256:" + "a" * 64, tamper=False):
    root = tmp_path / "pkgstage" / "mesflow-deploy-agent-release"
    root.mkdir(parents=True)
    tar_name = f"mesflow-deploy-agent_{version.replace('-docker-runtime','')}.tar"
    (root / tar_name).write_bytes(b"fake-tar-bytes")
    manifest = {"type": "mesflow-agent-release", "version": version,
                "image": "mesflow-deploy-agent:test", "image_digest": image_id, "image_id": image_id,
                "bundle": tar_name, "api_protocol_version": 2, "capabilities": ["image_release_receive"]}
    (root / "agent-release.json").write_text(json.dumps(manifest))
    lines = []
    for fname in (tar_name, "agent-release.json"):
        h = hashlib.sha256((root / fname).read_bytes()).hexdigest()
        lines.append(f"{h}  {fname}")
    if tamper:
        lines[0] = "0" * 64 + f"  {tar_name}"
    (root / "checksums.txt").write_text("\n".join(lines) + "\n")
    zip_path = tmp_path / "AGENT_UPDATE.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for f in root.iterdir():
            z.write(f, f"mesflow-deploy-agent-release/{f.name}")
    return zip_path, manifest


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.updater = load_updater(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_artifact_extracts_and_verifies(self):
        zip_path, manifest = build_artifact(self.tmp_path)
        workdir = self.tmp_path / "work"; workdir.mkdir()
        got_manifest, bundle = self.updater.extract_and_verify_artifact(zip_path.read_bytes(), workdir)
        self.assertEqual(got_manifest["version"], manifest["version"])
        self.assertTrue(bundle.is_file())

    def test_tampered_checksum_is_rejected(self):
        zip_path, _ = build_artifact(self.tmp_path, tamper=True)
        workdir = self.tmp_path / "work2"; workdir.mkdir()
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            self.updater.extract_and_verify_artifact(zip_path.read_bytes(), workdir)

    def test_missing_manifest_is_rejected(self):
        bad_zip = self.tmp_path / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as z:
            z.writestr("root/README.txt", "nope")
        workdir = self.tmp_path / "work3"; workdir.mkdir()
        with self.assertRaises(ValueError):
            self.updater.extract_and_verify_artifact(bad_zip.read_bytes(), workdir)

    def test_unsafe_zip_paths_are_rejected(self):
        evil_zip = self.tmp_path / "evil.zip"
        with zipfile.ZipFile(evil_zip, "w") as z:
            z.writestr("../escape.txt", "nope")
        workdir = self.tmp_path / "work4"; workdir.mkdir()
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.updater.extract_and_verify_artifact(evil_zip.read_bytes(), workdir)


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode; self.stdout = stdout; self.stderr = stderr


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.updater = load_updater(self.tmp_path)
        self.updater.write_compose_env({"AGENT_IMAGE": "mesflow-deploy-agent:8.8.8", "SERVER_ROLE": "PRODUCTION_TEST",
                                          "AGENT_BIND_IP": "127.0.0.1", "AGENT_PORT": "8090"})
        self.commands = []

    def tearDown(self):
        self.tmp.cleanup()

    def _patched_run(self, health_sequence):
        """health_sequence: list of (agent_version, server_role) dicts to return
        from successive `curl .../health` calls, exhausted -> last repeats."""
        calls = {"health_idx": 0}

        def fake_run(cmd, capture_output=True, text=True, timeout=None, cwd=None):
            self.commands.append(list(cmd))
            if cmd[:2] == ["docker", "load"]:
                return _FakeCompletedProcess(0, "Loaded image", "")
            if cmd[:3] == ["docker", "image", "inspect"]:
                return _FakeCompletedProcess(0, self.expected_id, "")
            if cmd[:2] == ["docker", "compose"] and "up" in cmd:
                return _FakeCompletedProcess(self.compose_up_rc, "", "")
            if cmd[0] == "curl":
                idx = min(calls["health_idx"], len(health_sequence) - 1)
                calls["health_idx"] += 1
                payload = health_sequence[idx]
                if payload is None:
                    return _FakeCompletedProcess(1, "", "connection refused")
                return _FakeCompletedProcess(0, json.dumps(payload), "")
            raise AssertionError(f"unexpected command in test: {cmd}")

        return fake_run

    def test_successful_update_recreates_only_the_agent_service(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="9.9.9-docker-runtime")
        self.expected_id = manifest["image_id"]
        self.compose_up_rc = 0
        health_ok = {"agent_version": "9.9.9-docker-runtime", "server_role": "PRODUCTION_TEST"}
        with patch("subprocess.run", side_effect=self._patched_run([health_ok])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 5):
            result = self.updater.do_update(zip_path.read_bytes())
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["to_version"], "9.9.9-docker-runtime")
        self._assert_never_touched_other_services()
        self.assertEqual(self.updater.compose_env()["AGENT_IMAGE"], manifest["image"])

    def test_image_id_mismatch_is_rejected_before_any_cutover(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="9.9.9-docker-runtime")
        self.expected_id = "sha256:" + "f" * 64  # does not match manifest's declared id
        self.compose_up_rc = 0
        with patch("subprocess.run", side_effect=self._patched_run([{}])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 5):
            with self.assertRaisesRegex(ValueError, "IMAGE_ID_MISMATCH"):
                self.updater.do_update(zip_path.read_bytes())
        # Must never have attempted the cutover itself.
        self.assertFalse(any("up" in c and "compose" in c for c in self.commands))

    def test_failed_health_check_triggers_rollback_to_previous_image(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="9.9.9-docker-runtime")
        self.expected_id = manifest["image_id"]
        self.compose_up_rc = 0
        # New Agent never becomes healthy; rollback's own health check succeeds.
        with patch("subprocess.run", side_effect=self._patched_run([None, None,
                    {"agent_version": "irrelevant", "server_role": "PRODUCTION_TEST"}])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 3):
            result = self.updater.do_update(zip_path.read_bytes())
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertEqual(self.updater.compose_env()["AGENT_IMAGE"], "mesflow-deploy-agent:8.8.8")
        self._assert_never_touched_other_services()

    def test_rollback_without_a_previous_image_reports_rollback_failed(self):
        self.updater.write_compose_env({"SERVER_ROLE": "PRODUCTION_TEST"})  # no AGENT_IMAGE recorded
        result = self.updater._rollback("", "PRODUCTION_TEST", "TEST_REASON")
        self.assertEqual(result["status"], "ROLLBACK_FAILED")

    def _assert_never_touched_other_services(self):
        for cmd in self.commands:
            joined = " ".join(cmd)
            self.assertNotIn("mesflow-app", joined)
            self.assertNotIn("postgres", joined)
            self.assertNotIn("qa-center", joined)
            self.assertNotIn("nginx", joined)
            self.assertNotIn("down", cmd)
            self.assertNotIn("-v", cmd)
            if "compose" in cmd and "up" in cmd:
                self.assertIn("mesflow-deploy-agent", cmd)
                self.assertEqual(cmd[-1], "mesflow-deploy-agent", "must scope the up to exactly one service")

    def test_downgrade_is_rejected_before_any_mutation(self):
        # Installed tag is 8.8.8 (see setUp); uploading 1.0.0 is a downgrade.
        zip_path, _ = build_artifact(self.tmp_path, version="1.0.0-docker-runtime")
        self.expected_id = "sha256:" + "b" * 64
        self.compose_up_rc = 0
        with patch("subprocess.run", side_effect=self._patched_run([{}])):
            with self.assertRaisesRegex(ValueError, "DOWNGRADE_REJECTED"):
                self.updater.do_update(zip_path.read_bytes())
        self.assertEqual(self.commands, [], "must not touch docker at all once rejected as a downgrade")
        self.assertEqual(self.updater.compose_env()["AGENT_IMAGE"], "mesflow-deploy-agent:8.8.8")

    def test_downgrade_allowed_with_explicit_override(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="1.0.0-docker-runtime")
        self.expected_id = manifest["image_id"]
        self.compose_up_rc = 0
        health_ok = {"agent_version": "1.0.0-docker-runtime", "server_role": "PRODUCTION_TEST"}
        with patch("subprocess.run", side_effect=self._patched_run([health_ok])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 5):
            result = self.updater.do_update(zip_path.read_bytes(), allow_downgrade=True)
        self.assertEqual(result["status"], "SUCCESS")

    def test_same_version_is_not_treated_as_a_downgrade(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="8.8.8-docker-runtime")
        self.expected_id = manifest["image_id"]
        self.compose_up_rc = 0
        health_ok = {"agent_version": "8.8.8-docker-runtime", "server_role": "PRODUCTION_TEST"}
        with patch("subprocess.run", side_effect=self._patched_run([health_ok])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 5):
            result = self.updater.do_update(zip_path.read_bytes())
        self.assertEqual(result["status"], "SUCCESS")

    def test_concurrent_update_is_rejected(self):
        zip_path, manifest = build_artifact(self.tmp_path, version="9.9.9-docker-runtime")
        lock_path = self.updater.STATE_DIR / "update.lock"
        held = open(lock_path, "w")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "AGENT_UPDATE_ALREADY_RUNNING"):
                self.updater.do_update(zip_path.read_bytes())
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
        # Lock released afterward -- a follow-up call succeeds normally.
        self.expected_id = manifest["image_id"]
        self.compose_up_rc = 0
        health_ok = {"agent_version": "9.9.9-docker-runtime", "server_role": "PRODUCTION_TEST"}
        with patch("subprocess.run", side_effect=self._patched_run([health_ok])), \
             patch.object(self.updater, "HEALTH_POLL_TIMEOUT", 5):
            result = self.updater.do_update(zip_path.read_bytes())
        self.assertEqual(result["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
