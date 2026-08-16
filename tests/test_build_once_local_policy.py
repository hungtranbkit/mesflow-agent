import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


class BuildOnceLocalPolicyTests(unittest.TestCase):
    def test_source_build_is_allowed_only_on_enabled_dev_agent(self):
        with patch.object(agent, "SERVER_ROLE", "DEV"), patch.object(agent, "MESFLOW_BUILD_ENABLED", True):
            self.assertTrue(agent._source_build_allowed())
        for role, enabled in (("TEST", False), ("TEST", True), ("PRODUCTION", False), ("PRODUCTION", True), ("DEV", False)):
            with self.subTest(role=role, enabled=enabled), patch.object(agent, "SERVER_ROLE", role), patch.object(agent, "MESFLOW_BUILD_ENABLED", enabled):
                self.assertFalse(agent._source_build_allowed())

    def test_mesflow_legacy_source_release_is_rejected_on_target_before_compose(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            release = root / "release"
            (target / "runtime").mkdir(parents=True)
            (target / ".env").write_text("POSTGRES_DB=test\n", encoding="utf-8")
            release.mkdir()
            (release / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            with patch.object(agent, "TARGET_HOME", target), patch.object(agent, "DATA_DIR", root / "data"), \
                 patch.object(agent, "SERVER_ROLE", "TEST"), patch.object(agent, "MESFLOW_BUILD_ENABLED", False), \
                 patch.object(agent, "_compose_at") as compose:
                with self.assertRaisesRegex(RuntimeError, "TARGET_SOURCE_BUILD_FORBIDDEN"):
                    agent._stage_linux_release(release, io.StringIO())
            compose.assert_not_called()

    def test_qa_target_never_falls_back_to_source_build(self):
        with patch.object(agent, "SERVER_ROLE", "PRODUCTION"), patch.object(agent, "MESFLOW_BUILD_ENABLED", False), \
             patch.object(agent, "_qa_compose") as compose:
            with self.assertRaisesRegex(RuntimeError, "TARGET_SOURCE_BUILD_FORBIDDEN"):
                agent._qa_docker_start(io.StringIO(), build=True)
        compose.assert_not_called()

    def test_qa_immutable_start_uses_no_build(self):
        calls = []
        with patch.object(agent, "SERVER_ROLE", "TEST"), patch.object(agent, "MESFLOW_BUILD_ENABLED", False), \
             patch.object(agent, "_qa_compose", side_effect=lambda args, **kwargs: calls.append(args)):
            agent._qa_docker_start(io.StringIO(), build=False)
        self.assertEqual(calls, [["up", "-d", "--no-build", "--force-recreate", "mesflow-qa-center"]])

    def test_release_builders_emit_immutable_image_archives(self):
        workspace = Path(__file__).resolve().parents[2]
        scripts = {
            "MESFlow": workspace / "mesflow/scripts/build-release.sh",
            "QA Center": workspace / "qa-center/scripts/build-release.sh",
            "Deploy Agent": workspace / "deploy-agent/scripts/build-agent-release.sh",
        }
        for component, script in scripts.items():
            with self.subTest(component=component):
                text = script.read_text(encoding="utf-8")
                self.assertIn("docker save", text)
                self.assertIn("sha256", text.lower())
                self.assertTrue("already exists" in text or "refusing" in text.lower())

    def test_agent_advertises_build_once_contract(self):
        self.assertIn("build_once_local", agent.AGENT_CAPABILITIES)

    def test_deployment_platform_reports_receive_only_target_policy(self):
        with patch.object(agent, "load_state", return_value={}), \
             patch.object(agent, "mes_status", return_value={"version": "1", "healthy": True}), \
             patch.object(agent, "qa_status", return_value={"version": "1", "online": True}), \
             patch.object(agent, "_release_summary", return_value={}), \
             patch.object(agent, "_qa_release_summary", return_value={}), \
             patch.object(agent, "_latest_agent_release", return_value={}), \
             patch.object(agent, "_deployment_platform_store") as store:
            store.return_value.list.return_value = []
            policy = agent._deployment_platform_summary()["artifact_policy"]
        self.assertEqual(policy["build_environment"], "LOCAL")
        self.assertFalse(policy["test_rebuild"])
        self.assertFalse(policy["production_rebuild"])
        self.assertEqual(policy["target_actions"], ["receive", "verify", "load", "activate", "health", "rollback"])


if __name__ == "__main__":
    unittest.main()
