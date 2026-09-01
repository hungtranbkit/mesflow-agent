import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const base = process.env.AGENT_BROWSER_BASE || 'http://mesflow-agent-browser-test:8090/agent';
const agentPassword = process.env.AGENT_BROWSER_PASSWORD || '';
const outputDir = process.env.AGENT_BROWSER_OUTPUT || '';
if (!agentPassword) throw new Error('AGENT_BROWSER_PASSWORD is required');

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1366, height: 768 } });
const page = await context.newPage();
const consoleErrors = [];
page.on('console', message => {
  if (message.type() === 'error' && !message.text().includes('status of 401')) consoleErrors.push(message.text());
});
page.on('pageerror', error => consoleErrors.push(error.message));

try {
  await page.goto(`${base}/login`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="password"]').fill(agentPassword);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith('/login')),
    page.getByRole('button', { name: 'Đăng nhập' }).click(),
  ]);

  await page.goto(`${base}/releases?tab=deploy`, { waitUntil: 'domcontentloaded' });
  const card = page.locator('#mesflowTutorialCard');
  await card.scrollIntoViewIfNeeded();
  await card.locator('#tutorialUsername').fill('admin');
  await card.locator('#tutorialPassword').fill('definitely-invalid-tutorial-password');
  const beforeUrl = page.url();
  const invalidResponse = page.waitForResponse(response => response.url().includes('/api/tutorial/check-login'));
  await card.locator('#tutorialCheckLoginButton').click();
  const invalid = await invalidResponse;
  await card.locator('#tutorialAuthResult').filter({ hasText: 'ĐĂNG NHẬP THẤT BẠI' }).waitFor();
  if (invalid.status() !== 401) throw new Error(`Expected real invalid credential HTTP 401, got ${invalid.status()}`);
  if (page.url() !== beforeUrl) throw new Error('Check Login unexpectedly navigated/reloaded the page');

  await page.route('**/api/tutorial/check-login', async route => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, authenticated: true, username: 'tutorial-user', status: 200, message: 'Đăng nhập MESFlow thành công' }) });
  });
  await card.locator('#tutorialUsername').fill('tutorial-user');
  await card.locator('#tutorialPassword').fill('mock-valid-only');
  await card.locator('#tutorialCheckLoginButton').click();
  await card.locator('#tutorialAuthResult').filter({ hasText: 'ĐĂNG NHẬP HỢP LỆ' }).waitFor();
  if (await card.locator('#tutorialExportButton').isDisabled()) throw new Error('Export button stayed disabled after valid auth feedback');

  await page.route('**/api/tutorial/export', async route => {
    await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ ok: true, job_id: 'tutorial-browser-job', status: 'queued', job: { job_id: 'tutorial-browser-job', status: 'queued', stage: 'queued', progress: 0, current: 0, total: 14, message: 'Đã xếp hàng xuất video' } }) });
  });
  await page.route('**/api/tutorial/job/tutorial-browser-job', async route => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, job: { job_id: 'tutorial-browser-job', status: 'running', stage: 'recording', progress: 42, current: 6, total: 14, current_item: '05_session', message: 'Đang quay 6/14: 05_session', elapsed_seconds: 123 } }) });
  });
  await card.locator('#tutorialExportButton').click();
  await card.locator('#tutorialJobStatus').filter({ hasText: 'RUNNING' }).waitFor({ timeout: 5000 });
  await card.locator('#tutorialJobStage').filter({ hasText: 'RECORDING' }).waitFor();
  await card.locator('#tutorialJobCount').filter({ hasText: '6 / 14' }).waitFor();
  if (outputDir) {
    mkdirSync(outputDir, { recursive: true });
    await card.screenshot({ path: `${outputDir}/tutorial-release-1366.png` });
  }

  await page.route('**/api/tutorial/job/current', async route => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, job: { job_id: 'tutorial-browser-job', status: 'running', stage: 'recording', progress: 42, current: 6, total: 14, current_item: '05_session', message: 'Đang quay 6/14: 05_session', elapsed_seconds: 192, log_tail: 'TUTORIAL_PROGRESS stage=recording current=6 total=14 item=05_session' } }) });
  });
  await page.goto(`${base}/tutorial/monitor`, { waitUntil: 'domcontentloaded' });
  await page.locator('#tutorialStatus').filter({ hasText: 'RUNNING' }).waitFor({ timeout: 5000 });
  await page.locator('#tutorialStage').filter({ hasText: 'RECORDING' }).waitFor();
  await page.locator('#tutorialCount').filter({ hasText: '6 / 14' }).waitFor();
  if (outputDir) {
    await page.screenshot({ path: `${outputDir}/tutorial-monitor-1366.png`, fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('#tutorialStatus').filter({ hasText: 'RUNNING' }).waitFor({ timeout: 5000 });
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.ops-shell')).paddingLeft === '0px');
    await page.screenshot({ path: `${outputDir}/tutorial-monitor-390.png`, fullPage: true });
  }
  if (consoleErrors.length) throw new Error(`Browser console errors: ${consoleErrors.join(' | ')}`);

  console.log(JSON.stringify({
    tutorial_card_visible: true,
    invalid_auth_http: invalid.status(),
    invalid_feedback_inline: true,
    no_unexpected_navigation: true,
    valid_feedback_mocked: true,
    export_http_202_mocked: true,
    main_progress_visible: true,
    monitor_polling_visible: true,
    console_errors: 0,
  }));
} finally {
  await browser.close();
}
