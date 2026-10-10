// EVOGIT_FIXED_HOST v1 -- zero-dependency application host. Do not edit.
//
// The ARC-Bench harness runs `npm run start` inside backend/ and probes
// GET /api/health before the Playwright suite. This host owns everything that
// must never break: the port, the health endpoint, static hosting of
// frontend/dist with SPA fallback, and crash isolation. Application behaviour
// lives in backend/src/app.js, loaded below. If that module fails to load or
// throws, the UI is still served and /api/* answers with a JSON error, so a
// backend defect can never take the whole application down.
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

const port = Number(process.env.PORT || process.env.ARCBENCH_WEB_PORT || process.env.ARC_WEB_PORT || 3000);
const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');
const dataDir = path.resolve(process.env.EVOGIT_DATA_DIR || path.join(__dirname, '../data'));
const MAX_BODY = 5 * 1024 * 1024;

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.map': 'application/json; charset=utf-8',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon', '.webp': 'image/webp', '.avif': 'image/avif', '.txt': 'text/plain; charset=utf-8',
  '.xml': 'application/xml; charset=utf-8', '.webmanifest': 'application/manifest+json', '.woff': 'font/woff',
  '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.pdf': 'application/pdf',
  '.wasm': 'application/wasm', '.csv': 'text/csv; charset=utf-8',
};

const BUILD_MISSING_PAGE = '<!doctype html><html lang="en"><head><meta charset="utf-8" /><title>Frontend Build Missing</title></head>' +
  '<body><main><h1>Frontend build missing</h1><p>Build frontend/ before starting the backend.</p></main></body></html>';

const hostState = { api_present: false, api_loaded: false, api_error: null, init_error: null, runtime_errors: [] };
// Local acceptance runs only (the agent sets EVOGIT_TEST_RESET=1): POST /api/__evogit_reset restores the
// data written by init() and reloads backend/src, so every local test starts from the seeded state.
// Without the variable the endpoint does not exist.
const TEST_RESET = process.env.EVOGIT_TEST_RESET === '1';
let seedSnapshot = null;

function recordRuntimeError(kind, error) {
  const text = kind + ': ' + (error && error.stack ? error.stack : String(error));
  console.error('[host] ' + text);
  hostState.runtime_errors.push(text.slice(0, 2000));
  if (hostState.runtime_errors.length > 20) hostState.runtime_errors.shift();
}

process.on('uncaughtException', (error) => recordRuntimeError('uncaughtException', error));
process.on('unhandledRejection', (error) => recordRuntimeError('unhandledRejection', error));

function ensureDataDir() {
  try { fs.mkdirSync(dataDir, { recursive: true }); } catch (error) { recordRuntimeError('dataDir', error); }
}

function safeName(name) {
  const clean = String(name || '').replace(/[^A-Za-z0-9._-]/g, '_');
  return clean.endsWith('.json') ? clean : clean + '.json';
}

function loadJson(name, fallback) {
  const file = path.join(dataDir, safeName(name));
  try {
    if (!fs.existsSync(file)) return typeof fallback === 'function' ? fallback() : fallback;
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    recordRuntimeError('loadJson ' + name, error);
    return typeof fallback === 'function' ? fallback() : fallback;
  }
}

function saveJson(name, value) {
  ensureDataDir();
  const file = path.join(dataDir, safeName(name));
  const temp = file + '.' + process.pid + '.' + Date.now() + '.tmp';
  fs.writeFileSync(temp, JSON.stringify(value, null, 2));
  fs.renameSync(temp, file);
  return value;
}

function sendJson(res, status, value) {
  if (res.writableEnded) return true;
  const body = JSON.stringify(value === undefined ? null : value);
  if (!res.headersSent) res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body);
  return true;
}

function parseCookies(header) {
  const cookies = {};
  String(header || '').split(';').forEach((part) => {
    const index = part.indexOf('=');
    if (index < 0) return;
    const key = part.slice(0, index).trim();
    if (!key) return;
    try { cookies[key] = decodeURIComponent(part.slice(index + 1).trim()); } catch (error) { cookies[key] = part.slice(index + 1).trim(); }
  });
  return cookies;
}

function appendCookie(res, text) {
  const existing = res.getHeader('Set-Cookie');
  const list = existing ? (Array.isArray(existing) ? existing : [String(existing)]) : [];
  list.push(text);
  res.setHeader('Set-Cookie', list);
}

function setCookie(res, name, value, options = {}) {
  let text = name + '=' + encodeURIComponent(String(value)) + '; Path=' + (options.path || '/') + '; SameSite=' + (options.sameSite || 'Lax');
  if (options.httpOnly !== false) text += '; HttpOnly';
  if (options.maxAge !== undefined) text += '; Max-Age=' + Math.floor(Number(options.maxAge));
  appendCookie(res, text);
}

function clearCookie(res, name, options = {}) {
  appendCookie(res, name + '=; Path=' + (options.path || '/') + '; Max-Age=0; SameSite=Lax; HttpOnly');
}

function readBody(req) {
  if (req.__evogitBody) return req.__evogitBody;
  req.__evogitBody = new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) { reject(new Error('Request body too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('error', reject);
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      req.rawBody = raw;
      if (!raw) { resolve({}); return; }
      const type = String(req.headers['content-type'] || '');
      if (type.includes('application/x-www-form-urlencoded')) { resolve(Object.fromEntries(new URLSearchParams(raw))); return; }
      try { resolve(JSON.parse(raw)); } catch (error) { resolve(type.includes('json') ? {} : { text: raw }); }
    });
  });
  return req.__evogitBody;
}

function matcher(req, pathname) {
  return function match(method, pattern) {
    const wanted = String(method || '*').toUpperCase();
    if (wanted !== '*' && wanted !== 'ANY' && wanted !== String(req.method || 'GET').toUpperCase()) return null;
    const expected = String(pattern).replace(/\/+$/, '').split('/').filter(Boolean);
    const actual = pathname.replace(/\/+$/, '').split('/').filter(Boolean);
    const params = {};
    for (let index = 0; index < expected.length; index += 1) {
      const part = expected[index];
      if (part === '*') { params['*'] = actual.slice(index).map(decodeURIComponent).join('/'); return params; }
      if (index >= actual.length) return null;
      if (part.startsWith(':')) {
        try { params[part.slice(1)] = decodeURIComponent(actual[index]); } catch (error) { params[part.slice(1)] = actual[index]; }
      } else if (part !== actual[index]) return null;
    }
    return expected.length === actual.length ? params : null;
  };
}

let api = null;
const appModulePath = path.join(__dirname, 'app.js');
const sharedContext = { dataDir, loadJson, saveJson, sendJson, setCookie, clearCookie, readBody };

async function loadApi() {
  ensureDataDir();
  if (!fs.existsSync(appModulePath)) return;
  hostState.api_present = true;
  try {
    let loaded = require(appModulePath);
    if (loaded && loaded.default && !loaded.handle && typeof loaded !== 'function') loaded = loaded.default;
    if (typeof loaded === 'function') loaded = { handle: loaded };
    if (!loaded || typeof loaded.handle !== 'function') throw new Error('backend/src/app.js must export handle(req, res, ctx)');
    api = loaded;
    hostState.api_loaded = true;
  } catch (error) {
    hostState.api_error = String(error && error.stack ? error.stack : error).slice(0, 4000);
    console.error('[host] backend/src/app.js failed to load: ' + hostState.api_error);
    return;
  }
  if (typeof api.init === 'function') {
    try { await api.init({ ...sharedContext }); } catch (error) {
      hostState.init_error = String(error && error.stack ? error.stack : error).slice(0, 4000);
      console.error('[host] app.init failed: ' + hostState.init_error);
    }
  }
}

function snapshotData(dir, prefix, out) {
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (error) { return out; }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    const rel = prefix ? prefix + '/' + entry.name : entry.name;
    if (entry.isDirectory()) snapshotData(full, rel, out);
    else if (entry.isFile() && !entry.name.endsWith('.tmp')) { try { out[rel] = fs.readFileSync(full); } catch (error) { /* skip */ } }
  }
  return out;
}

async function resetApp() {
  if (!seedSnapshot) throw new Error('no seed snapshot');
  try { fs.rmSync(dataDir, { recursive: true, force: true }); } catch (error) { recordRuntimeError('reset rm', error); }
  ensureDataDir();
  for (const [rel, content] of Object.entries(seedSnapshot)) {
    const target = path.join(dataDir, rel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
  }
  for (const key of Object.keys(require.cache)) {
    if (key.startsWith(__dirname + path.sep) && key !== __filename) delete require.cache[key];
  }
  api = null;
  hostState.api_loaded = false;
  hostState.api_error = null;
  hostState.init_error = null;
  await loadApi();
}

function resolveStaticFile(pathname) {
  let decoded;
  try { decoded = decodeURIComponent(pathname); } catch (error) { return null; }
  const candidate = path.resolve(frontendDistPath, decoded.replace(/^\/+/, ''));
  if (candidate !== frontendDistPath && !candidate.startsWith(frontendDistPath + path.sep)) return null;
  return candidate;
}

function serveSpa(req, res) {
  fs.readFile(path.join(frontendDistPath, 'index.html'), (error, content) => {
    if (error) { res.writeHead(503, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(BUILD_MISSING_PAGE); return; }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
    res.end(req.method === 'HEAD' ? undefined : content);
  });
}

function serveStatic(req, res, pathname) {
  const filePath = resolveStaticFile(pathname);
  if (!filePath) { serveSpa(req, res); return; }
  fs.stat(filePath, (error, stats) => {
    if (error || !stats.isFile()) {
      // Missing hashed assets must not be answered with HTML.
      if (/^\/assets\//.test(pathname)) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('Not Found'); return; }
      serveSpa(req, res);
      return;
    }
    const type = MIME_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': type, 'Content-Length': stats.size, 'Cache-Control': 'no-cache' });
    if (req.method === 'HEAD') { res.end(); return; }
    fs.createReadStream(filePath).pipe(res);
  });
}

async function handleApi(req, res, url) {
  if (!api) {
    if (hostState.api_present) sendJson(res, 500, { error: 'API module failed to load', detail: hostState.api_error });
    else sendJson(res, 404, { code: 404, message: 'Not Found' });
    return;
  }
  const ctx = {
    ...sharedContext,
    method: String(req.method || 'GET').toUpperCase(),
    url,
    path: url.pathname,
    query: url.searchParams,
    cookies: parseCookies(req.headers.cookie),
    match: matcher(req, url.pathname),
  };
  try {
    const handled = await api.handle(req, res, ctx);
    if (!res.writableEnded && !handled) sendJson(res, 404, { code: 404, message: 'Not Found' });
  } catch (error) {
    recordRuntimeError('handle ' + req.method + ' ' + url.pathname, error);
    if (!res.writableEnded) sendJson(res, 500, { error: String(error && error.message ? error.message : error) });
  }
}

const server = http.createServer((req, res) => {
  const host = req.headers.host || '127.0.0.1:' + port;
  let url;
  try { url = new URL(req.url || '/', 'http://' + host); } catch (error) { sendJson(res, 400, { code: 400, message: 'Bad Request' }); return; }
  const pathname = url.pathname;
  if (pathname === '/api/health') { sendJson(res, 200, { code: 200, message: 'Backend Ready' }); return; }
  if (pathname === '/api/__host') { sendJson(res, 200, hostState); return; }
  if (TEST_RESET && pathname === '/api/__evogit_reset' && req.method === 'POST') {
    resetApp().then(() => sendJson(res, 200, { ok: true }), (error) => sendJson(res, 500, { error: String(error && error.message || error) }));
    return;
  }
  if (pathname === '/api' || pathname.startsWith('/api/')) { handleApi(req, res, url); return; }
  const method = req.method || 'GET';
  if (method !== 'GET' && method !== 'HEAD') { sendJson(res, 405, { code: 405, message: 'Method Not Allowed' }); return; }
  if (!fs.existsSync(frontendDistPath)) { res.writeHead(503, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(BUILD_MISSING_PAGE); return; }
  serveStatic(req, res, pathname);
});

// A hanging init() must never keep the port closed.
let startupSettled = false;
const initTimeout = new Promise((resolve) => setTimeout(() => {
  if (!startupSettled) hostState.init_error = hostState.init_error || 'init() still pending after 15s';
  resolve();
}, 15000).unref());
const startup = loadApi().catch((error) => recordRuntimeError('loadApi', error)).finally(() => {
  startupSettled = true;
  if (TEST_RESET) seedSnapshot = snapshotData(dataDir, '', {});
});
Promise.race([startup, initTimeout]).finally(() => {
  server.listen(port, () => { console.log('Backend listening at http://127.0.0.1:' + port); });
});
