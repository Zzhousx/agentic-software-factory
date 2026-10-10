// Prints {"ok":true,"executablePath":""} when Playwright's own browser launches, else the first
// launchable system/cached Chromium, else {"ok":false,"reason":...}.
const { chromiumCandidates } = require('./locator_runtime.cjs');
(async () => {
  const { chromium } = require(process.argv[2]);
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  const errors = [];
  const attempt = async (executablePath) => {
    const browser = await chromium.launch(executablePath ? { headless: true, args, executablePath } : { headless: true, args });
    await browser.close();
  };
  try { await attempt(''); process.stdout.write(JSON.stringify({ ok: true, executablePath: '' }) + '\n'); return; } catch (error) { errors.push(String(error.message || error).split('\n')[0]); }
  for (const candidate of chromiumCandidates().filter((p) => p !== process.env.EVOGIT_CHROMIUM_PATH)) {
    try { await attempt(candidate); process.stdout.write(JSON.stringify({ ok: true, executablePath: candidate }) + '\n'); return; } catch (error) { errors.push(candidate + ': ' + String(error.message || error).split('\n')[0]); }
  }
  process.stdout.write(JSON.stringify({ ok: false, reason: errors.join(' | ').slice(0, 800) }) + '\n');
})().catch((error) => process.stdout.write(JSON.stringify({ ok: false, reason: String(error).slice(0, 400) }) + '\n'));
