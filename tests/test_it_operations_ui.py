import re
from unittest.mock import patch

import agent


def authenticated_client():
    client=agent.app.test_client()
    with client.session_transaction() as state:
        state["authenticated"]=True
        state["csrf"]="ui-test"
    return client


def test_default_home_is_operational_overview():
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        response=authenticated_client().get("/")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    assert "Tổng quan vận hành" in body
    assert "Trạng thái hệ thống" in body
    assert "Release &amp; Deploy" in body
    assert 'class="ops-sidebar"' in body
    assert "releaseBuildButton" not in body


def test_release_manager_moved_without_losing_primary_actions():
    # Real user report (reports/RELEASE_DEPLOY_UX_CLEANUP.md): the stage
    # already shown as the primary "Next Action" button (mesNextBuild/...)
    # used to ALSO appear a second time inside "Thao tác khác", a literal
    # on-screen duplicate. That one stage's secondary button is now omitted
    # -- reachable via its primary id instead -- so each of the 4 stage
    # actions must exist via EITHER its "releaseXButton" secondary id or the
    # primary "mesNextX" id, never neither.
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        response=authenticated_client().get("/releases")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    # ESP Firmware Builder / ESP Tutorial moved off this page entirely
    # (task: ESP_KIOSK_UI_SEPARATION) -- their ids belong to /kiosk/firmware
    # and /kiosk/tutorial now, not the Release page.
    for element_id in ("espBuildButton","espTutorialButton"):
        assert f'id="{element_id}"' not in body
    for secondary_id,primary_id in (("releaseBuildButton","mesNextBuild"),("releaseLocalButton","mesNextLocal"),
                                     ("releaseTestButton","mesNextTest"),("releaseProdButton","mesNextProd")):
        assert f'id="{secondary_id}"' in body or f'id="{primary_id}"' in body
    assert "/api/release-manager/build" in body
    assert "/api/release-manager/deploy-local" in body
    assert "/api/release-manager/promote-test" in body
    assert "/api/release-manager/promote-production" in body


def test_release_deploy_tab_never_shows_the_next_action_stage_twice():
    # A fresh install's Next Action is Build (nothing built yet) -- the
    # real bug: "Build Release" rendered as both the primary button
    # (mesNextBuild) and again inside "Thao tác khác" (releaseBuildButton).
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        body=authenticated_client().get("/releases?tab=deploy").get_data(as_text=True)
    assert 'id="mesNextBuild"' in body
    assert 'id="releaseBuildButton"' not in body
    # The other 3 stage actions remain reachable via "Thao tác khác".
    for secondary_id in ("releaseLocalButton","releaseTestButton","releaseProdButton"):
        assert f'id="{secondary_id}"' in body


def test_release_deploy_tab_collapses_stacked_accordions_into_one_per_card():
    # Real user report: "Thao tác khác" / "Xem chi tiết artifact / evidence"
    # / "Production Preflight" used to be 3 separate <details> toggles per
    # card, reading as tabs nested inside a tab. Now grouped under one.
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        body=authenticated_client().get("/releases?tab=deploy").get_data(as_text=True)
    assert body.count("Thao tác khác &amp; chi tiết") == 2  # MESFlow + QA Center cards
    assert "Chi tiết artifact / evidence" in body
    assert "Dịch vụ QA Center (Start/Stop)" in body


def test_operations_deep_link_selects_requested_workspace():
    response=authenticated_client().get("/ops?view=docker")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    assert "const initialView=\"docker\"" in body
    assert 'id="docker"' in body
    assert "Thao tác nâng cao" in body


def test_old_ota_url_redirects_to_kiosk_ota_page():
    # Back-compat for the pre-ESP_KIOSK_UI_SEPARATION bookmark/sidebar URL.
    response=authenticated_client().get("/ota")
    assert response.status_code==302
    assert response.headers["Location"].endswith("/kiosk/ota")


def test_kiosk_ota_workspace_reuses_existing_ota_contracts():
    response=authenticated_client().get("/kiosk/ota")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    assert "ESP Kiosk" in body and "OTA" in body
    # v2 scoped flow: overview -> release upload -> preview -> one forced batch
    for path in ("/api/esp-ota/overview","/api/esp-ota/targets","/api/esp-ota/releases","/api/esp-ota/preview","/api/esp-ota/force"):
        assert path in body
    assert "Cập nhật bắt buộc tất cả ESP của MESFlow này" in body


def test_unknown_operations_view_falls_back_safely():
    response=authenticated_client().get("/ops?view=not-a-page")
    assert response.status_code==200
    assert 'const initialView="overview"' in response.get_data(as_text=True)


def test_operations_tabs_have_unique_targets_and_visible_feedback():
    response=authenticated_client().get("/ops?view=alerts")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    assert body.count('id="alerts"')==1
    assert 'id="healthAlerts"' in body
    assert 'id="opsFeedback"' in body
    assert "feedback('Đang tải cảnh báo" in body


def test_sidebar_release_links_use_real_tabs_not_anchors():
    # Deployment History and Agent Update moved from anchor-scroll targets
    # on one giant page to real ?tab= navigations that replace the main
    # workspace content -- see reports/RELEASE_DEPLOY_UX_CLEANUP.md.
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        response=authenticated_client().get("/releases")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    # (Not asserting a leading path segment: url_for() prepends URL_PREFIX
    # unconditionally, but the test client's bare "/releases" request never
    # sets SCRIPT_NAME via the reverse-proxy-prefix middleware -- a
    # pre-existing test-environment quirk, not a routing bug.)
    assert re.search(r'href="[^"]*\?tab=deploy"', body)
    assert re.search(r'href="[^"]*\?tab=history"', body)
    assert re.search(r'href="[^"]*\?tab=agent-update"', body)
    # ESP Firmware Builder / ESP Tutorial moved off this page entirely
    # (task: ESP_KIOSK_UI_SEPARATION) -- their old anchors must not remain.
    for target in ("espBuilderCard","espTutorialCard"):
        assert f'id="{target}"' not in body


def test_old_esp_kiosk_release_section_links_redirect_to_kiosk_pages():
    # Real user report (pre-separation): "ESP Kiosk · Firmware"/"· Hướng
    # dẫn" sidebar links pointed at /releases?tab=deploy&section=... . Both
    # cards moved to their own pages -- old bookmarks/links must redirect,
    # not 404 or silently drop the section.
    builder=authenticated_client().get("/releases?tab=deploy&section=esp-builder")
    tutorial=authenticated_client().get("/releases?tab=deploy&section=esp-tutorial")
    assert builder.status_code==302 and builder.headers["Location"].endswith("/kiosk/firmware")
    assert tutorial.status_code==302 and tutorial.headers["Location"].endswith("/kiosk/tutorial")


def test_release_page_has_no_duplicate_in_page_tab_bar():
    # Real user report: the sidebar's TRIỂN KHAI group already links to
    # ?tab=deploy/history/agent-update -- release/index.html used to ALSO
    # render its own <nav class="release-tabs"> with the exact same 3
    # destinations, both visible on screen at once. Sidebar is now the
    # single navigation source; the page heading names the active tab
    # instead (same convention as ops.html already uses for its 10 views).
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        deploy=authenticated_client().get("/releases?tab=deploy").get_data(as_text=True)
        history=authenticated_client().get("/releases?tab=history").get_data(as_text=True)
        agent_update=authenticated_client().get("/releases?tab=agent-update").get_data(as_text=True)
    for body in (deploy,history,agent_update):
        assert 'class="release-tabs"' not in body
    assert "Release &amp; Deploy · Release Deploy" in deploy
    assert "Release &amp; Deploy · Deployment History" in history
    assert "Release &amp; Deploy · Agent Update" in agent_update


def test_history_and_agent_update_tabs_render_own_scoped_content():
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        history=authenticated_client().get("/releases?tab=history")
        agent_update=authenticated_client().get("/releases?tab=agent-update")
    hbody=history.get_data(as_text=True); abody=agent_update.get_data(as_text=True)
    assert history.status_code==200 and agent_update.status_code==200
    assert 'id="histRows"' in hbody
    assert 'id="securitySettings"' in abody
    # Each tab's content is genuinely scoped -- not the same giant page with
    # everything present at once.
    assert 'id="releaseBuildButton"' not in hbody
    assert 'id="releaseBuildButton"' not in abody
    assert 'id="histRows"' not in abody


def test_shared_release_script_helpers_load_before_tab_partials():
    # Real bug found+fixed (reports/RELEASE_DEPLOY_UX_CLEANUP.md): inline
    # <script> tags execute in document order, and the History tab's own
    # script calls apiFetch()/CSRF immediately (unconditional `load();`) --
    # if the shared helper <script> defining them rendered AFTER
    # {% include tab_partial %}, that call silently threw "apiFetch is not
    # defined" (caught by its own try/catch) and History always showed
    # "failed to load" with zero real network requests. The shared helpers
    # must appear before any tab partial in the rendered HTML.
    with patch.object(agent,"_release_contamination",return_value={"contaminated":False}):
        response=authenticated_client().get("/releases?tab=history")
    body=response.get_data(as_text=True)
    assert response.status_code==200
    assert body.index("const CSRF=")<body.index('id="histRows"')
    assert body.index("async function apiFetch")<body.index("histRows.innerHTML")


def test_agent_update_local_row_never_gets_a_production_update_button():
    # Real bug found+fixed (reports/RELEASE_DEPLOY_UX_CLEANUP.md): the
    # template's role mapping defaulted anything that wasn't literally
    # "PRODUCTION TEST" to role='PRODUCTION' -- including the LOCAL/DEV row
    # -- so LOCAL rendered a red "Update Production Agent" button wired to
    # send role=PRODUCTION. /api/release-manager/update-agent only accepts
    # role=PRODUCTION_TEST/PRODUCTION (INVALID_ROLE otherwise), so LOCAL
    # must render with no update-agent-btn at all.
    fake_rows=[
        {"name":"LOCAL","role":"DEV","agent_version":"1.0.0","configured":True,
         "compatibility":{"status":"COMPATIBLE","reason":"","missing":[]}},
        {"name":"PRODUCTION TEST","role":"PRODUCTION_TEST","agent_version":"1.0.0","configured":True,
         "compatibility":{"status":"COMPATIBLE","reason":"","missing":[]},"updater_configured":True,"latest_agent_version":"1.0.0"},
        {"name":"PRODUCTION","role":None,"configured":False,
         "compatibility":{"status":"NOT_CONFIGURED","reason":"No target configured","missing":[]}},
    ]
    with patch.object(agent,"_target_agents_table",return_value=fake_rows):
        client=authenticated_client()
        csrf=re.search(r'const CSRF="([^"]*)"',client.get("/releases?tab=agent-update").get_data(as_text=True)).group(1)
        client.post("/api/release-manager/target-agents",headers={"X-CSRF-Token":csrf,"X-Requested-With":"XMLHttpRequest"})
        body=client.get("/releases?tab=agent-update").get_data(as_text=True)
    local_block=body[body.index('class="name">LOCAL'):body.index('class="name">TEST')]
    assert "update-agent-btn" not in local_block
    assert 'data-role="PRODUCTION"' not in local_block
    test_block=body[body.index('class="name">TEST'):body.index('class="name">PRODUCTION')]
    assert 'data-role="PRODUCTION_TEST"' in test_block


def test_agent_update_job_heading_reflects_finished_state():
    # Real bug found+fixed (reports/RELEASE_DEPLOY_UX_CLEANUP.md): the
    # finished-job card always said "Đang cập nhật" (updating...) even for
    # a job that had already reached SUCCESS/FAILED/ROLLED_BACK.
    def mutate(s):
        s["agent_update_job"]={"status":"SUCCESS","message":"Agent updated and verified healthy",
            "role":"PRODUCTION_TEST","version":"9.9.9-docker-runtime"}
    agent.update_state(mutate)
    try:
        body=authenticated_client().get("/releases?tab=agent-update").get_data(as_text=True)
    finally:
        agent.update_state(lambda s: s.pop("agent_update_job",None))
    assert "Đang cập nhật Agent PRODUCTION_TEST" not in body
    assert "Cập nhật Agent PRODUCTION_TEST" in body


def test_kiosk_pages_are_now_real_separate_routes():
    # ESP Kiosk used to be one page with client-side local tabs
    # (data-local-tab="fleet"/"ota"); it is 4 real, bookmarkable routes now
    # (task: ESP_KIOSK_UI_SEPARATION), each with its own sidebar entry.
    overview=authenticated_client().get("/kiosk")
    ota=authenticated_client().get("/kiosk/ota").get_data(as_text=True)
    firmware=authenticated_client().get("/kiosk/firmware").get_data(as_text=True)
    tutorial=authenticated_client().get("/kiosk/tutorial").get_data(as_text=True)
    assert overview.status_code==200
    assert 'data-local-tab' not in ota
    assert 'id="devices"' in ota and 'id="releaseForm"' in ota and 'id="forceAll"' in ota
    assert 'id="espBuildButton"' in firmware
    assert 'id="espTutorialUploadForm"' in tutorial
