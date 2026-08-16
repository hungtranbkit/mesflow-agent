from pathlib import Path
import ast
import re
ROOT=Path(__file__).resolve().parents[1]

def test_version_sync():
    version=(ROOT/'VERSION.txt').read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", version), version
    s=(ROOT/'agent.py').read_text()
    assert f'AGENT_VERSION="{version}"' in s

def test_mes_upload_auto_deploys():
    # /upload is the current Build-Once artifact transfer route (used by
    # _remote_agent_upload machine-to-machine calls and Deploy Local), not
    # the removed legacy manual-ZIP-upload UI -- see
    # reports/RELEASE_DEPLOY_UX_CLEANUP.md.
    s=(ROOT/'agent.py').read_text()
    block=s[s.index('@app.post("/upload")'):s.index('@app.post("/deploy/<version>")')]
    assert 'start_deploy(v)' in block
    assert 'Đã upload và bắt đầu deploy MESFlow' in block

def test_legacy_source_zip_deploy_routes_removed():
    # Legacy full-source-ZIP manual upload/deploy/delete routes (both the
    # MESFlow "dry-run/delete" manual-UI actions and the entire QA Center
    # legacy deploy path) were removed as dead UI-only code -- the current
    # Build-Once architecture never used them for machine-to-machine
    # transfer. See reports/RELEASE_DEPLOY_UX_CLEANUP.md for the audit.
    s=(ROOT/'agent.py').read_text()
    for removed_route in (
        '@app.post("/qa/upload")',
        '@app.post("/qa/deploy/<version>")',
        '@app.post("/qa/delete/<version>")',
        '@app.post("/dry-run/<version>")',
        '@app.post("/delete/<version>")',
    ):
        assert removed_route not in s, f"legacy route should have been removed: {removed_route}"
    for removed_func in (
        'def locate_qa_root',
        'def list_qa_releases',
        'def run_qa_job',
        'def start_qa_deploy',
        'def set_qa_step',
        'def validate_mes_release_contract',
    ):
        assert removed_func not in s, f"dead legacy function should have been removed: {removed_func}"
    # /deploy/<version> and /qa-release/* are NOT legacy -- they are the
    # real machine-to-machine triggers _run_promote_test()/QA promote-test
    # depend on, and must still be present.
    assert '@app.post("/deploy/<version>")' in s
    assert '@app.post("/qa-release/upload")' in s

def test_legacy_upload_ui_removed_from_templates():
    # The old giant single-page templates/index.html (with its manual
    # source-ZIP <form> pickers) was deleted; the Release Deploy tab has no
    # arbitrary-file-upload form for MESFlow/QA source ZIPs.
    assert not (ROOT/'templates/index.html').exists()
    deploy_tab=(ROOT/'templates/release/_deploy_tab.html').read_text()
    assert 'mesUploadButton' not in deploy_tab
    assert 'qaUploadButton' not in deploy_tab

def test_agent_parses():
    ast.parse((ROOT/'agent.py').read_text())
