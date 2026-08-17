"""Docker List Disappear UI Bug (reports/DOCKER_LIST_DISAPPEAR_UI_FIX.md,
reports/DOCKER_ADVANCED_ACTION_UI_BUG.md,
reports/DOCKER_NORMAL_PROFILE_LIST_DISAPPEAR_FIX.md).

Real-browser regression for Operations -> Docker: clicking an ordinary row
(anywhere in the row, not just the "Thao tác nâng cao" toggle) or the
toggle itself must never hide/clear the Docker list. Live reproduction on
the real local host across two independent investigation rounds -- every
real container row, multiple viewports, filtered/idle/sequential-click
variations, and (this round) plain-row clicks on a non-interactive cell --
never showed the reported "entire Docker list disappears" symptom against
the current source. This test guards the concrete hardening applied
(data-attributes + closest() delegation instead of fragile inline onclick
string interpolation, explicit type="button") plus the exact acceptance
criteria: row count and list visibility unchanged across a plain row
click, opening/closing the advanced toggle, and switching to a second
row's toggle -- zero page/console errors, no reload required, no
duplicate DOM ids.

A third round (DOCKER_NORMAL_PROFILE_LIST_DISAPPEAR_FIX.md) found what the
first two missed: both prior rounds always tested with a continuously
valid session. loadDocker() unconditionally ran `dockers = d.items || []`
-- so ANY soft API failure (most concretely `/api/ops/docker` returning
`{"ok":false,"error":"AUTH_REQUIRED"}`, exactly what it returns once the
session cookie is no longer valid -- reproduced directly via curl with no
cookie) wiped the whole list, even though nothing about the real Docker
state had changed. dockerAction() (the function a Restart/Stop/Start click
inside "Thao tác nâng cao" calls) always calls loadDocker() afterward, so
one row-level click with a stale session was enough to blank the list --
this is `test_stale_session_docker_refresh_preserves_the_list` below.

Spawns a REAL second `agent.py` process (matches tests/test_p5_fleet_routes.py's
convention -- Flask's test client can't be driven by a real browser) and
drives it with Playwright. Docker listing here is read-only (`docker ps -a`
against the real host daemon, same as the existing /api/ops/docker route);
this test never clicks Restart/Stop/Start, so it can never mutate a real
container -- only the toggle itself is exercised, matching task section 10.

Skips cleanly (not a failure) if Playwright/its browser binaries aren't
available in this environment -- this is a real-browser regression, not a
requirement Deploy Agent's own runtime depends on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed in this environment")

from conftest import ADMIN_PASSWORD, spawn_agent, stop_agent

ROOT = Path(__file__).resolve().parents[1]
PORT = 18260


@pytest.fixture(scope="module")
def browser():
    try:
        with playwright_sync_api.sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            yield b
            b.close()
    except Exception as exc:  # missing browser binaries, sandboxing, etc.
        pytest.skip(f"Chromium not launchable in this environment: {exc}")


def test_docker_advanced_action_toggle_never_hides_the_list(tmp_path_factory, browser):
    tmp_path = tmp_path_factory.mktemp("docker-ui-bug")
    proc, base_url = spawn_agent(tmp_path, PORT, server_role="DEV")
    try:
        console_errors: list[str] = []
        page_errors: list[str] = []
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(base_url + "/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", ADMIN_PASSWORD)
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("button")

        page.goto(base_url + "/ops?view=docker", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # real docker ps -a round trip

        rows = page.locator("#dockerRows tr")
        row_count = rows.count()
        assert row_count > 1, "expected more than one real Docker row on this host"
        assert page.locator("#dockerRows").is_visible()
        assert page.locator("#docker .tablewrap").is_visible()

        # No duplicate DOM ids anywhere on the page (task section 5) --
        # repeated per-row controls must key off data-* attributes, never
        # a reused global id.
        ids = page.evaluate("[...document.querySelectorAll('[id]')].map(e => e.id)")
        duplicates = {i for i in ids if ids.count(i) > 1}
        assert not duplicates, f"duplicate DOM ids found: {duplicates}"

        navigations_seen = []
        page.on("framenavigated", lambda frame: navigations_seen.append(frame.url) if frame == page.main_frame else None)

        # Plain row click (task's explicit new emphasis: "clicking ANY
        # Docker row", not just the advanced toggle) -- click the Name
        # cell, a non-interactive part of the row, and confirm nothing
        # about the list changes.
        first_row = rows.first
        first_row.locator("td").first.click()
        page.wait_for_timeout(300)
        assert rows.count() == row_count, "row count changed after a plain row click"
        assert page.locator("#dockerRows").is_visible(), "Docker list hidden after a plain row click"
        assert page.locator("#docker .tablewrap").is_visible(), "Docker list wrapper hidden after a plain row click"

        def toggle_and_assert(row_index: int):
            row = rows.nth(row_index)
            summary = row.locator("summary")
            assert summary.count() == 1, f"row {row_index} missing its own advanced-action toggle"

            summary.click()
            page.wait_for_timeout(300)
            assert rows.count() == row_count, f"row count changed after opening row {row_index}"
            assert page.locator("#dockerRows").is_visible(), f"Docker list hidden after opening row {row_index}"
            assert row.locator("details[open]").count() == 1, f"row {row_index} advanced area did not open"
            for other in range(row_count):
                if other != row_index:
                    assert rows.nth(other).is_visible(), f"row {other} disappeared while row {row_index} was open"

            summary.click()  # close again
            page.wait_for_timeout(300)
            assert rows.count() == row_count, f"row count changed after closing row {row_index}"
            assert page.locator("#dockerRows").is_visible(), f"Docker list hidden after closing row {row_index}"

        toggle_and_assert(0)
        if row_count > 1:
            toggle_and_assert(1)

        assert navigations_seen == [], f"unexpected document navigation during toggling: {navigations_seen}"
        assert page_errors == [], f"page errors during Docker toggle: {page_errors}"
        assert console_errors == [], f"console errors during Docker toggle: {console_errors}"

        page.close()
    finally:
        stop_agent(proc)


def test_stale_session_docker_refresh_preserves_the_list(tmp_path_factory, browser):
    """reports/DOCKER_NORMAL_PROFILE_LIST_DISAPPEAR_FIX.md -- the actual
    root cause the first two rounds missed, because both always tested
    with a continuously valid session. A long-lived "normal" browser tab
    is exactly the case most likely to have its session cookie go stale
    mid-visit (logout in another tab, cookie eviction, browser session
    restore, an Agent restart that predates a persisted secret_key, etc.);
    a fresh Incognito login essentially never hits this within a short
    session -- matching the reported Normal-vs-Incognito split without
    needing any caching/service-worker/localStorage mechanism at all.

    Reproduces the exact failure directly: clears the session cookie
    (simulating an expired/invalid session) with the Docker list already
    showing real rows, then triggers the same loadDocker() a Restart/Stop/
    Start click would trigger afterward. /api/ops/docker now genuinely
    returns {"ok": false, "error": "AUTH_REQUIRED"} (no "items" key) --
    confirmed live via curl during this investigation. Before the fix,
    `dockers = d.items || []` ran unconditionally and wiped the list to
    empty on exactly this response shape. After the fix, loadDocker()
    only replaces `dockers` when d.ok !== false, so the previously
    fetched rows must stay exactly as they were.
    """
    tmp_path = tmp_path_factory.mktemp("docker-ui-stale-session")
    proc, base_url = spawn_agent(tmp_path, PORT + 1, server_role="DEV")
    try:
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(base_url + "/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", ADMIN_PASSWORD)
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("button")

        page.goto(base_url + "/ops?view=docker", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        rows = page.locator("#dockerRows tr")
        row_count_before = rows.count()
        assert row_count_before > 0, "expected at least one real Docker row on this host"

        # Simulate an expired/invalid session WITHOUT navigating away, so
        # the already-rendered list stays exactly as it was -- reproducing
        # "the list was showing fine, then this one action broke it."
        page.context.clear_cookies()

        # Trigger the exact function a Restart/Stop/Start click calls
        # afterward (loadDocker()) via the Docker section's own "Làm mới"
        # button -- scoped to #docker specifically, since the page has
        # several other sections with their own same-labeled buttons.
        page.click("#docker .toolbar button:has-text('Làm mới')")
        page.wait_for_timeout(800)

        row_count_after = page.locator("#dockerRows tr").count()
        assert row_count_after == row_count_before, (
            f"Docker list was wiped by a stale-session refresh: "
            f"{row_count_before} rows before, {row_count_after} after"
        )
        assert page.locator("#dockerRows").is_visible(), "Docker list hidden after a stale-session refresh"

        page.close()
    finally:
        stop_agent(proc)
