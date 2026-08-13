import io
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent


class DeploySafetyTests(unittest.TestCase):
    def setUp(self):
        # The managed Windows runner can create TemporaryDirectory itself but
        # denies access to children created inside it.  Keep the same isolated
        # filesystem assertions while placing cases under a normal workspace
        # test directory.  Linux and unrestricted runners retain tempfile.
        if __import__("os").name == "nt":
            root = Path(__file__).resolve().parents[1] / ".test-tmp" / f"deploy-safety-{uuid.uuid4().hex}"
            root.mkdir(parents=True)
            self.tmp = None
        else:
            self.tmp = tempfile.TemporaryDirectory()
            root = Path(self.tmp.name)
        self.root = root
        self.target = root / "target"
        self.data = root / "data"
        self.logs = self.data / "logs"
        self.target.mkdir(); self.logs.mkdir(parents=True)
        (self.target / ".env").write_text("POSTGRES_DB=mesflow\n", encoding="utf-8")
        (self.target / "runtime" / "postgres-v65").mkdir(parents=True)
        (self.target / "runtime" / "postgres-v65" / "KEEP").write_text("db", encoding="utf-8")
        (self.target / "certs").mkdir()
        self.old = {
            "TARGET_HOME": agent.TARGET_HOME, "DATA_DIR": agent.DATA_DIR,
            "DEPLOY_TX_FILE": agent.DEPLOY_TX_FILE, "DEPLOY_LOCK_FILE": agent.DEPLOY_LOCK_FILE,
            "STATE_FILE": agent.STATE_FILE, "LOG_DIR": agent.LOG_DIR, "AGENT_LOG": agent.AGENT_LOG,
        }
        agent.TARGET_HOME = self.target; agent.DATA_DIR = self.data
        agent.DEPLOY_TX_FILE = self.data / "deploy_transaction.json"
        agent.DEPLOY_LOCK_FILE = self.data / "deploy.lock"
        agent.STATE_FILE = self.data / "state.json"
        agent.LOG_DIR = self.logs
        agent.AGENT_LOG = self.logs / "agent.log"

    def tearDown(self):
        for k, v in self.old.items(): setattr(agent, k, v)
        if self.tmp is not None:
            self.tmp.cleanup()
        else:
            shutil.rmtree(self.root)

    def release(self, version="2"):
        release = self.root / f"release-{version}"; release.mkdir()
        (release / "VERSION.txt").write_text(version, encoding="utf-8")
        (release / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        (release / "app").mkdir(); (release / "app" / "new.txt").write_text("new", encoding="utf-8")
        return release

    def install_old(self):
        (self.target / "VERSION.txt").write_text("1", encoding="utf-8")
        (self.target / "compose.yml").write_text("services: {mesflow: {}, postgres: {}}\n", encoding="utf-8")
        (self.target / "old.txt").write_text("old", encoding="utf-8")

    def test_runtime_exists_is_preserved_and_package_runtime_is_ignored(self):
        release = self.release(); (release / "runtime").mkdir(); (release / "runtime" / "BAD").write_text("bad")
        agent._replace_nonpersistent(release)
        self.assertEqual((self.target / "runtime/postgres-v65/KEEP").read_text(), "db")
        self.assertFalse((self.target / "runtime/BAD").exists())

    def test_compose_is_not_lost_during_replacement(self):
        self.install_old(); agent._replace_nonpersistent(self.release())
        self.assertTrue((self.target / "compose.yml").is_file())

    def test_backup_contains_exact_old_compose_and_excludes_database(self):
        self.install_old(); backup = agent._create_linux_backup(io.StringIO())
        self.assertEqual((backup / "VERSION.txt").read_text(), "1")
        self.assertTrue((backup / "compose.yml").is_file())
        self.assertFalse((backup / "runtime").exists())

    def test_invalid_compose_fails_in_staging_before_cutover(self):
        release = self.release(); self.install_old()
        with patch.object(agent, "_compose_at", side_effect=RuntimeError("invalid compose")):
            with self.assertRaisesRegex(RuntimeError, "invalid compose"): agent._stage_linux_release(release, io.StringIO())
        self.assertEqual((self.target / "VERSION.txt").read_text(), "1")

    def test_staging_failure_does_not_touch_production(self):
        self.install_old(); release = self.release()
        with patch.object(agent, "_copy_release_tree", side_effect=OSError("stage failed")):
            with self.assertRaisesRegex(OSError, "stage failed"): agent._stage_linux_release(release, io.StringIO())
        self.assertTrue((self.target / "old.txt").is_file())

    def test_stop_never_uses_compose_down(self):
        self.install_old(); calls=[]
        def record(root,log,args,**kwargs): calls.append(args); return 0
        with patch.object(agent, "_compose_at", side_effect=record): agent._stop_application_tier(io.StringIO())
        self.assertEqual(calls, [["stop", "mesflow"]])

    def test_start_names_only_application_owned_services(self):
        self.install_old(); calls=[]
        def record(root,log,args,**kwargs): calls.append(args); return 0
        # _start_stack refuses to run unless persistent PostgreSQL already
        # reports healthy (it must never be recreated just because the
        # application tier redeploys) — mock that pre-flight check so this
        # test exercises the compose invocation itself, not real Docker.
        with patch.object(agent, "_compose_at", side_effect=record), \
             patch.object(agent, "_docker_mes_state", return_value={"service_health": {"postgres": "healthy"}}):
            agent._start_stack(io.StringIO())
        # --no-deps + only "mesflow" is intentional: the gateway and
        # PostgreSQL are neither recreated nor reset by an application
        # deploy (see _start_stack's docstring comment).
        self.assertEqual(calls, [["up", "-d", "--no-build", "--no-deps", "mesflow"]])
        self.assertFalse(any("postgres" in args for args in calls))

    def test_legacy_docker_helpers_do_not_target_gateway(self):
        self.install_old(); calls=[]
        def record(log,args,**kwargs): calls.append(args); return 0
        with patch.object(agent, "os", SimpleNamespace(name="posix")), patch.object(agent, "_docker_compose", side_effect=record), patch.object(agent, "port_open", return_value=False):
            agent.stop_mes(io.StringIO())
            agent.start_mes(io.StringIO())
        self.assertEqual(calls, [["stop", "mesflow"], ["up", "-d", "--build", "postgres", "mesflow"]])
        self.assertFalse(any("nginx" in args for args in calls))

    def test_exception_after_stop_can_restore_exact_old_version(self):
        self.install_old(); backup=agent._create_linux_backup(io.StringIO()); agent._replace_nonpersistent(self.release())
        with patch.object(agent, "_stop_application_tier"), patch.object(agent, "_start_stack"), patch.object(agent, "mes_status", return_value={"version":"1","healthy":True}):
            agent._rollback_linux_source(backup, "1", io.StringIO())
        self.assertEqual((self.target / "VERSION.txt").read_text(), "1")
        self.assertTrue((self.target / "old.txt").is_file())

    def test_unhealthy_application_fails_rollback_healthcheck(self):
        self.install_old(); backup=agent._create_linux_backup(io.StringIO())
        with patch.object(agent, "_stop_application_tier"), patch.object(agent, "_start_stack"), patch.object(agent, "mes_status", return_value={"version":"1","healthy":False}), patch.object(agent, "MES_VERIFY_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "healthcheck failed"): agent._rollback_linux_source(backup, "1", io.StringIO())

    def test_nginx_is_not_part_of_application_health(self):
        self.install_old()
        completed = []
        def run(cmd, **kwargs):
            completed.append(cmd)
            class Result:
                returncode=0; stderr=""
                stdout=("postgres\nmesflow\n" if "--services" in cmd else
                        "healthy\n" if cmd[:2] == ["docker", "inspect"] else "container-id\n")
            return Result()
        # Patch only os.name, not the whole `os` module: _docker_mes_state()
        # also calls os.environ.copy(), which a wholesale
        # SimpleNamespace(name="posix") replacement doesn't have — that
        # AttributeError was being silently swallowed by _docker_mes_state's
        # broad except clause, masking this test's real assertions.
        with patch.object(agent.os, "name", "posix"), patch.object(agent.subprocess, "run", side_effect=run):
            state=agent._docker_mes_state()
        self.assertTrue(state["healthy"])
        self.assertNotIn("nginx", state["service_health"])
        self.assertFalse(any("nginx" in command for command in completed))

    def test_failed_deploy_and_rollback_never_target_gateway(self):
        self.install_old(); backup=agent._create_linux_backup(io.StringIO()); calls=[]
        def record(root,log,args,**kwargs): calls.append(args); return 0
        with patch.object(agent, "_compose_at", side_effect=record), patch.object(agent, "mes_status", return_value={"version":"1","healthy":True}), \
             patch.object(agent, "_docker_mes_state", return_value={"service_health": {"postgres": "healthy"}}), \
             patch.object(agent, "MES_VERIFY_TIMEOUT", 1):
            agent._rollback_linux_source(backup, "1", io.StringIO())
        self.assertTrue(calls)
        self.assertFalse(any("nginx" in args for args in calls))
        self.assertFalse(any("down" in args or "rm" in args for args in calls))

    def test_deploy_lock_rejects_second_deployment(self):
        release=self.release(); releases=self.root/"releases"; releases.mkdir(); release.rename(releases/"2")
        old_releases=agent.RELEASES_DIR; agent.RELEASES_DIR=releases
        agent.DEPLOY_LOCK_FILE.write_text("existing")
        try:
            with self.assertRaisesRegex(RuntimeError,"lock exists"): agent.start_deploy("2",dry_run=True)
        finally: agent.RELEASES_DIR=old_releases

    def test_restart_during_cutover_recovers_from_transaction(self):
        self.install_old(); backup=agent._create_linux_backup(io.StringIO()); agent._replace_nonpersistent(self.release())
        agent.save_json(agent.DEPLOY_TX_FILE,{"status":"running","phase":"CUTOVER","backup":str(backup),"from_version":"1"})
        with patch.object(agent, "_rollback_linux_source") as rollback:
            self.assertTrue(agent.recover_interrupted_deploy())
        rollback.assert_called_once()

    def test_persistent_validation_rejects_unsafe_runtime_symlink(self):
        runtime=self.target/"runtime"
        for child in runtime.rglob("*"):
            if child.is_file(): child.unlink()
        (runtime/"postgres-v65").rmdir(); runtime.rmdir()
        runtime.symlink_to(self.target/"certs",target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,"Unsafe persistent runtime"): agent._validate_persistent_paths()

    def test_dry_run_validation_never_enters_cutover(self):
        self.install_old()
        releases = self.root / "releases"; releases.mkdir()
        release = self.release(); release.rename(releases / "2")
        stage = self.root / "validated-stage"; stage.mkdir()
        with patch.object(agent, "RELEASES_DIR", releases), \
             patch.object(agent, "os", SimpleNamespace(name="posix")), \
             patch.object(agent, "mes_status", return_value={"version":"1"}), \
             patch.object(agent, "_stage_linux_release", return_value=stage), \
             patch.object(agent, "_stop_application_tier") as stop, \
             patch.object(agent, "_start_stack") as start:
            agent.run_job("2", dry_run=True)
        self.assertEqual(agent.load_state()["job"]["status"], "success")
        stop.assert_not_called(); start.assert_not_called()
        self.assertEqual((self.target/"runtime/postgres-v65/KEEP").read_text(), "db")


if __name__ == "__main__": unittest.main()
