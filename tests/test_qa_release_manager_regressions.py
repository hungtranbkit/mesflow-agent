from __future__ import annotations

import ast
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import agent


ROOT = Path(__file__).resolve().parents[1]


def test_agent_has_no_duplicate_top_level_function_definitions():
    tree = ast.parse((ROOT / "agent.py").read_text(encoding="utf-8"))
    names: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.setdefault(node.name, []).append(node.lineno)
    duplicates = {name: lines for name, lines in names.items() if len(lines) > 1}
    assert not duplicates, f"duplicate top-level function definitions: {duplicates}"


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_legacy_container_takeover_removes_only_exact_qa_container_and_preserves_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(); marker = runtime / "state.json"; marker.write_text('{"campaign":"keep"}', encoding="utf-8")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return _completed(stdout="mesflow-qa\n")
        if cmd == ["docker", "rm", "-f", "mesflow-qa-center"]:
            return _completed(stdout="mesflow-qa-center\n")
        raise AssertionError(cmd)

    log = io.StringIO()
    with patch.object(agent, "QA_HOME", tmp_path), patch.object(agent.subprocess, "run", side_effect=run):
        assert agent._qa_takeover_legacy_container_if_needed(log) is True

    assert calls == [
        ["docker", "inspect", "mesflow-qa-center", "--format", '{{ index .Config.Labels "com.docker.compose.project" }}'],
        ["docker", "rm", "-f", "mesflow-qa-center"],
    ]
    assert marker.read_text(encoding="utf-8") == '{"campaign":"keep"}'
    assert all("mesflow-app" not in call and "postgres" not in call and "deploy-agent" not in call for call in calls)


def test_normal_subsequent_release_does_not_manually_remove_container(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(stdout=f"{tmp_path.name.replace('_', '-').lower()}\n")

    with patch.object(agent, "QA_HOME", tmp_path), patch.object(agent.subprocess, "run", side_effect=run):
        assert agent._qa_takeover_legacy_container_if_needed(io.StringIO()) is False

    assert len(calls) == 1 and calls[0][:2] == ["docker", "inspect"]
    assert not any(call[:3] == ["docker", "rm", "-f"] for call in calls)


def test_legacy_takeover_deploys_frozen_version_and_digest_without_touching_runtime(tmp_path):
    version = "1.19.12"
    image_id = "sha256:" + "5" * 64
    staging = tmp_path / "staging"; release = staging / version; release.mkdir(parents=True)
    qa_home = tmp_path / "qa-home"; runtime = qa_home / "runtime"; runtime.mkdir(parents=True)
    marker = runtime / "state.json"; marker.write_text('{"run":"persistent"}', encoding="utf-8")
    metadata = {"version": version, "image": f"mesflow-qa-center:{version}", "image_id": image_id,
                "image_digest": image_id, "bundle": f"QACenter_{version}.tar"}
    (release / "release.json").write_text(__import__("json").dumps(metadata), encoding="utf-8")
    (release / "compose.yml").write_text("services: {mesflow-qa-center: {image: mesflow-qa-center:1.19.12}}\n", encoding="utf-8")
    (release / "checksums.txt").write_text("", encoding="utf-8")
    (release / metadata["bundle"]).write_bytes(b"frozen-image")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "load"]: return _completed(stdout="Loaded\n")
        if cmd[:3] == ["docker", "image", "inspect"]: return _completed(stdout=image_id + "\n")
        if cmd[:2] == ["docker", "inspect"]: return _completed(stdout="mesflow-qa\n")
        if cmd == ["docker", "rm", "-f", "mesflow-qa-center"]: return _completed(stdout="mesflow-qa-center\n")
        raise AssertionError(cmd)

    compose = Mock(return_value=0)
    with patch.object(agent, "QA_IMAGE_RELEASES_DIR", staging), patch.object(agent, "QA_HOME", qa_home), \
         patch.object(agent, "validate_qa_image_release_contract", return_value=(version, metadata)), \
         patch.object(agent.subprocess, "run", side_effect=run), \
         patch.object(agent, "_qa_release_compose", compose), \
         patch.object(agent, "qa_http_json", return_value=({"ok": True, "version": version}, 200)), \
         patch.object(agent.time, "sleep", return_value=None):
        agent._qa_deploy_from_staging(version, io.StringIO())

    assert marker.read_text(encoding="utf-8") == '{"run":"persistent"}'
    assert agent.load_json(qa_home / "release.json", {})["image_id"] == image_id
    assert ["docker", "rm", "-f", "mesflow-qa-center"] in calls
    compose.assert_called_once_with(ANY,
                                    ["up", "-d", "--no-build", "--force-recreate", "mesflow-qa-center"])
    assert not any("mesflow-app" in call or "postgres" in call or "deploy-agent" in call for call in calls)


def test_qa_capability_contract_is_distinct_and_mesflow_defaults_unchanged():
    expected_qa = {"qa_image_release_receive", "qa_deploy", "digest_verify", "deploy_status", "health_status"}
    expected_mes = {"image_release_receive", "digest_verify", "remote_deploy", "deploy_status", "health_status"}
    assert agent.QA_REQUIRED_PROMOTION_CAPABILITIES == expected_qa
    assert agent.REQUIRED_PROMOTION_CAPABILITIES == expected_mes
    status = {"agent_version": agent.AGENT_VERSION, "api_protocol_version": agent.API_PROTOCOL_VERSION,
              "capabilities": sorted(expected_mes)}
    assert agent._agent_compatibility(status)["status"] == "COMPATIBLE"
    qa = agent._agent_compatibility(status, agent.QA_REQUIRED_PROMOTION_CAPABILITIES)
    assert qa["status"] == "INCOMPATIBLE"
    assert qa["missing"] == ["qa_deploy", "qa_image_release_receive"]


def test_qa_promotion_missing_capability_stops_before_upload(tmp_path):
    package = tmp_path / "QACenter_1.2.3.deploy.zip"; package.write_bytes(b"frozen")
    artifact = {"version": "1.2.3", "package": str(package), "package_sha256": agent.sha(package),
                "image_id": "sha256:" + "a" * 64, "digest": "sha256:" + "a" * 64}
    status = {"agent_version": "old", "api_protocol_version": 2, "server_role": "PRODUCTION_TEST",
              "build_enabled": False, "capabilities": sorted(agent.QA_REQUIRED_PROMOTION_CAPABILITIES - {"qa_deploy"})}
    upload = Mock()
    updates = []
    with patch.object(agent, "_qa_artifact_for_release", return_value=artifact), \
         patch.object(agent, "_qa_release_contamination", return_value={"contaminated": False}), \
         patch.object(agent, "_qa_load_promotion_state", return_value={"version": "1.2.3", "local_pass": True}), \
         patch.object(agent, "_production_test_target", return_value=("http://target", "admin", "pw")), \
         patch.object(agent, "_remote_agent_client", return_value=(object(), "http://target", "csrf")), \
         patch.object(agent, "_remote_agent_get_json", return_value=status), \
         patch.object(agent, "_remote_agent_qa_upload", upload), \
         patch.object(agent, "_qa_update_promotion_state"), \
         patch.object(agent, "qa_promote_test_update", side_effect=lambda state, message, **kw: updates.append((state, message))):
        agent._run_qa_promote_test("1.2.3")
    upload.assert_not_called()
    assert updates[-1][0] == "FAILED" and "TARGET_AGENT_INCOMPATIBLE" in updates[-1][1]


def test_qa_promotion_with_all_capabilities_reaches_upload_and_deploy(tmp_path):
    package = tmp_path / "QACenter_1.2.3.deploy.zip"; package.write_bytes(b"frozen")
    artifact = {"version": "1.2.3", "package": str(package), "package_sha256": agent.sha(package),
                "image_id": "sha256:" + "a" * 64, "digest": "sha256:" + "a" * 64}
    initial = {"agent_version": agent.AGENT_VERSION, "api_protocol_version": agent.API_PROTOCOL_VERSION,
               "server_role": "PRODUCTION_TEST", "build_enabled": False,
               "capabilities": sorted(agent.QA_REQUIRED_PROMOTION_CAPABILITIES)}
    finished = {**initial, "qa_image_deploy_job": {"status": "success", "version": "1.2.3"},
                "qa": {"online": True, "version": "1.2.3"}}
    upload, deploy = Mock(), Mock()
    with patch.object(agent, "_qa_artifact_for_release", return_value=artifact), \
         patch.object(agent, "_qa_release_contamination", return_value={"contaminated": False}), \
         patch.object(agent, "_qa_load_promotion_state", return_value={"version": "1.2.3", "local_pass": True}), \
         patch.object(agent, "_production_test_target", return_value=("http://target", "admin", "pw")), \
         patch.object(agent, "_remote_agent_client", return_value=(object(), "http://target", "csrf")), \
         patch.object(agent, "_remote_agent_get_json", return_value=initial), \
         patch.object(agent, "_remote_agent_status", return_value=finished), \
         patch.object(agent, "_remote_agent_qa_upload", upload), \
         patch.object(agent, "_remote_agent_qa_deploy", deploy), \
         patch.object(agent, "_qa_update_promotion_state"), \
         patch.object(agent, "qa_promote_test_update"), patch.object(agent, "_qa_cleanup_old_releases"), \
         patch.object(agent.time, "sleep", return_value=None):
        agent._run_qa_promote_test("1.2.3")
    upload.assert_called_once(); deploy.assert_called_once()


def test_qa_and_mesflow_promotion_state_files_are_independent():
    assert agent.QA_PROMOTION_STATE_FILE != agent.PROMOTION_STATE_FILE
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert 's["qa_promote_test_job"]' in source
    assert 's["promote_test_job"]' in source


def test_frozen_qa_11912_artifact_identity_and_metadata():
    release = ROOT.parent / "artifacts" / "qa-center" / "releases" / "1.19.12"
    package = release / "QACenter_1.19.12.deploy.zip"
    metadata = agent.load_json(release / "release.json", {})
    image_info = agent.load_json(release / "image-info.json", {})
    assert package.is_file()
    assert (release / "checksums.txt").is_file()
    assert (release / "PROMOTION.json").is_file()
    assert metadata["version"] == "1.19.12"
    assert metadata["image"] == "mesflow-qa-center:1.19.12"
    assert metadata["image_digest"] == "sha256:5dba3d98f0b33a889e3992f5c758d0961c6c19bc2e882de431d27df639a60e2e"
    assert image_info["digest"] == metadata["image_digest"]
    assert agent.sha(package) == "a49829b8c6a7674b58ea6a847878654ce0d026efd9cf981ca691848264edd4d4"
