// Drop-in replacement for '@playwright/test' in generated specs: on failure it attaches the
// page URL, an accessibility snapshot and browser errors so repairs see what the test saw.
import { test as base } from '@playwright/test';
export * from '@playwright/test';

export const test = base.extend<{ evogitCapture: void }>({
  evogitCapture: [async ({ page }, use, testInfo) => {
    // Start every test from the seeded state (local runs only; ignored when the host has no reset endpoint).
    if (process.env.EVOGIT_RESET === '1' && process.env.EVOGIT_BASE_URL) {
      try { await fetch(process.env.EVOGIT_BASE_URL + '/api/__evogit_reset', { method: 'POST' }); } catch { /* ignore */ }
    }
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(String((error as Error).message || error).slice(0, 400)));
    page.on('console', (message) => { if (message.type() === 'error') errors.push('console: ' + message.text().slice(0, 300)); });
    page.on('response', (response) => {
      const status = response.status();
      if (status >= 400 && status !== 401 && response.url().includes('/api/') && errors.length < 40)
        errors.push(`HTTP ${status} ${response.request().method()} ${new URL(response.url()).pathname}`);
    });
    await use();
    if (testInfo.status !== testInfo.expectedStatus) {
      let aria = '';
      try { aria = await page.locator('body').ariaSnapshot({ timeout: 2000 }); } catch { aria = ''; }
      await testInfo.attach('evogit-failure', {
        body: JSON.stringify({ url: page.url(), aria: aria.slice(0, 6000), errors: errors.slice(0, 12) }),
        contentType: 'application/json',
      });
    }
  }, { auto: true }],
});
