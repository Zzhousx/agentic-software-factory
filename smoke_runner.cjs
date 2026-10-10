// Browser render smoke: does each route render visible content without uncaught errors?
const { launchChromium } = require('./locator_runtime.cjs');
(async () => {
  const [baseUrl, modulePath, routesJson] = process.argv.slice(2);
  const { chromium } = require(modulePath);
  let browser;
  try { browser = await launchChromium(chromium); } catch (error) {
    process.stdout.write(JSON.stringify({ status: 'browser_unavailable', reason: String(error.message || error).slice(0, 400) }) + '\n');
    return;
  }
  const report = { status: 'executed', routes: [] };
  try {
    for (const route of JSON.parse(routesJson || '["/"]')) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (error) => errors.push(String(error.message || error).slice(0, 500)));
      let empty = false;
      try {
        await page.goto(baseUrl + route, { waitUntil: 'load', timeout: 15000 });
        await page.waitForTimeout(1200);
        const info = await page.evaluate(() => {
          const root = document.getElementById('root');
          const text = (document.body && document.body.innerText || '').trim();
          const controls = document.querySelectorAll('button,a,input,textarea,select,[role]').length;
          return { children: root ? root.children.length : 0, text: text.length, controls };
        });
        empty = info.children === 0 || (info.text === 0 && info.controls === 0);
        report.routes.push({ route, page_errors: errors, empty, info });
      } catch (error) {
        report.routes.push({ route, page_errors: errors.concat(['navigation: ' + String(error.message || error).slice(0, 300)]), empty: true });
      } finally { await context.close(); }
    }
  } finally { await browser.close(); }
  process.stdout.write(JSON.stringify(report) + '\n');
})().catch((error) => { process.stdout.write(JSON.stringify({ status: 'runner_error', reason: String(error).slice(0, 400) }) + '\n'); });
