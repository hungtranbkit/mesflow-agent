"""ESP Kiosk UI Separation (task: ESP_KIOSK_UI_SEPARATION).

Firmware Builder and Tutorial moved off the Release & Deploy page onto
their own /kiosk/* pages. These tests cover: the new routes render, old
bookmarked URLs redirect instead of 404ing/silently dropping state, the
Release page no longer renders ESP content, and -- the actual performance
point of this task -- each page's context builder never calls into the
other domain's expensive functions (release summaries / deployment
platform scans on kiosk pages, esp_tutorial_status()/ota.list_firmware()
on the release page).
"""
from unittest.mock import patch

import agent


def authenticated_client():
    client = agent.app.test_client()
    with client.session_transaction() as state:
        state["authenticated"] = True
        state["csrf"] = "ui-test"
    return client


def _boom(*_a, **_k):
    raise AssertionError("expensive cross-domain function was called")


def test_all_four_kiosk_routes_render():
    client = authenticated_client()
    for path, marker in (
        ("/kiosk", "ESP Kiosk"),
        ("/kiosk/ota", "id=\"devices\""),
        ("/kiosk/firmware", "id=\"espBuildButton\""),
        ("/kiosk/tutorial", "id=\"espTutorialUploadForm\""),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.get_data(as_text=True), path


def test_kiosk_sidebar_highlights_exactly_the_active_tab():
    client = authenticated_client()
    body = client.get("/kiosk/firmware").get_data(as_text=True)
    assert 'class="active"' in body
    # Exactly one ESP KIOSK nav entry active, and it's the Firmware one.
    import re
    actives = re.findall(r'class="active"[^>]*>([^<]+)<', body)
    assert "Firmware" in actives
    assert "Tổng quan" not in actives and "OTA" not in actives and "Hướng dẫn" not in actives


def test_old_bookmarked_urls_redirect_not_404():
    client = authenticated_client()
    old_ota = client.get("/ota")
    old_firmware = client.get("/releases?tab=deploy&section=esp-builder")
    old_tutorial = client.get("/releases?tab=deploy&section=esp-tutorial")
    assert old_ota.status_code == 302 and old_ota.headers["Location"].endswith("/kiosk/ota")
    assert old_firmware.status_code == 302 and old_firmware.headers["Location"].endswith("/kiosk/firmware")
    assert old_tutorial.status_code == 302 and old_tutorial.headers["Location"].endswith("/kiosk/tutorial")


def test_release_page_has_no_esp_content():
    with patch.object(agent, "_release_contamination", return_value={"contaminated": False}):
        body = authenticated_client().get("/releases?tab=deploy").get_data(as_text=True)
    for marker in ("espBuildButton", "espTutorialUploadForm", "espBuilderCard", "espTutorialCard"):
        assert marker not in body


def test_release_page_context_never_touches_esp_kiosk_backend():
    # The actual performance point of this task: opening the Release page
    # must not pay for esp_tutorial_status()'s file I/O or the OTA
    # firmware-store query, regardless of what those functions would do.
    with patch.object(agent, "_release_contamination", return_value={"contaminated": False}), \
         patch.object(agent, "esp_tutorial_status", side_effect=_boom), \
         patch.object(agent.ota, "list_firmware", side_effect=_boom):
        response = authenticated_client().get("/releases?tab=deploy")
    assert response.status_code == 200


def test_kiosk_overview_context_never_touches_release_backend():
    with patch.object(agent, "_release_summary", side_effect=_boom), \
         patch.object(agent, "_qa_release_summary", side_effect=_boom), \
         patch.object(agent, "_deployment_platform_summary", side_effect=_boom):
        response = authenticated_client().get("/kiosk")
    assert response.status_code == 200


def test_kiosk_firmware_and_tutorial_context_never_touch_release_backend():
    with patch.object(agent, "_release_summary", side_effect=_boom), \
         patch.object(agent, "_qa_release_summary", side_effect=_boom), \
         patch.object(agent, "_deployment_platform_summary", side_effect=_boom), \
         patch.object(agent, "mes_status", side_effect=_boom), \
         patch.object(agent, "qa_status", side_effect=_boom):
        firmware = authenticated_client().get("/kiosk/firmware")
        tutorial = authenticated_client().get("/kiosk/tutorial")
    assert firmware.status_code == 200
    assert tutorial.status_code == 200


def test_kiosk_firmware_page_reuses_existing_esp_builder_backend():
    # No new build endpoint was invented -- the button must still POST to
    # the original esp_builder_start/esp_builder_status routes.
    body = authenticated_client().get("/kiosk/firmware").get_data(as_text=True)
    assert "/api/esp-builder/start" in body
    assert "/api/esp-builder/status" in body


def test_esp_tutorial_upload_redirects_to_kiosk_tutorial_page():
    client = authenticated_client()
    response = client.post("/esp-kiosk-tutorial/upload", data={"csrf": "ui-test"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/kiosk/tutorial")


def test_release_live_status_endpoint_still_tracks_esp_job_keys():
    # /kiosk/firmware and /kiosk/tutorial poll job status via this SAME
    # shared, cheap endpoint -- it must not be duplicated per page.
    payload = agent._release_live_status_payload()
    for key in ("esp_builder_job", "esp_tutorial_job", "tutorial_job"):
        assert key in payload["jobs"]
