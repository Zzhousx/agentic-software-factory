"""Start the generated application the way ARC-Bench does and check it really serves.

925.V1 only ran `node --check` + `vite build`, and its browser runner started the server
from the project root and treated "application exited" as "browser unavailable". A
backend that crashed on start (missing package) or resolved paths from process.cwd()
therefore could be shipped despite failing runtime interactions.
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_BROWSER_STATE: dict = {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float = 5.0):
    request = urllib.request.Request(url, headers={"User-Agent": "evogit-smoke"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", "") if error.headers else "", error.read()


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "ARC_BENCH_"))}
    env.pop("MODEL", None)
    env.update(extra or {})
    return env


class RunningApp:
    def __init__(self, process, port, log_path, data_dir):
        self.process, self.port, self.log_path, self.data_dir = process, port, log_path, data_dir
        self.base_url = f"http://127.0.0.1:{port}"
        self.error = None

    def log_tail(self, limit: int = 3000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def host_state(self):
        try:
            status, _, body = _get(self.base_url + "/api/__host", 3)
            return json.loads(body) if status == 200 else None
        except Exception:
            return None


@contextlib.contextmanager
def running_app(root: Path, startup_seconds: float = 25.0, data_dir: Path | None = None, extra_env: dict | None = None):
    """Run backend/src/index.js with cwd=backend (== `npm run start`) and an isolated data dir."""
    root = Path(root)
    temp = Path(tempfile.mkdtemp(prefix="evogit-app-"))
    data = Path(data_dir) if data_dir else temp / "data"
    log_path = temp / "server.log"
    port = _free_port()
    node = shutil.which("node") or "node"
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen([node, "src/index.js"], cwd=root / "backend", stdout=log, stderr=subprocess.STDOUT,
                               env=child_env({"PORT": str(port), "EVOGIT_DATA_DIR": str(data), "NODE_ENV": "production",
                                              **(extra_env or {})}))
    app = RunningApp(process, port, log_path, data)
    try:
        deadline = time.monotonic() + startup_seconds
        while True:
            if process.poll() is not None:
                app.error = f"server process exited with code {process.returncode} during startup"
                break
            try:
                status, _, _ = _get(app.base_url + "/api/health", 1.0)
                if status == 200:
                    break
            except Exception:
                pass
            if time.monotonic() > deadline:
                app.error = f"GET /api/health did not return 200 within {int(startup_seconds)}s"
                break
            time.sleep(0.15)
        yield app
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(5)
        with contextlib.suppress(Exception):
            if process.poll() is None:
                process.kill()
        log.close()
        shutil.rmtree(temp, ignore_errors=True)


def ensure_playwright(deadline: float | None = None) -> dict:
    """Install (once) @playwright/test outside the application tree and find a launchable Chromium.

    Returns {'ok', 'module', 'cli', 'executable', 'reason'}; cached for the whole run."""
    if "tool" in _BROWSER_STATE:
        return _BROWSER_STATE["tool"]
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
    node = shutil.which("node") or "node"
    tool = {"ok": False, "module": None, "cli": None, "executable": "", "reason": ""}
    for folder in (ROOT / ".arc" / "evaluator", Path(tempfile.gettempdir()) / "evogit-evaluator"):
        module = folder / "node_modules" / "playwright"
        cli = folder / "node_modules" / "@playwright" / "test" / "cli.js"
        if not (module.exists() and cli.exists()):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                limit = 240 if deadline is None else max(5, min(240, deadline - time.monotonic()))
                subprocess.run([npm, "install", "--prefix", str(folder), "--no-audit", "--no-fund", "--no-save",
                                "--fetch-retries=2", "--fetch-timeout=60000", "@playwright/test@1.57.0"],
                               cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=limit)
            except Exception as error:
                tool["reason"] = f"npm install @playwright/test failed: {type(error).__name__}"
                continue
        if module.exists():
            tool.update(module=str(module), cli=str(cli if cli.exists() else module / "cli.js"), folder=str(folder))
            break
        tool["reason"] = "npm install @playwright/test did not produce node_modules/playwright"
    if tool["module"]:
        probe = _find_chromium(node, tool["module"])
        if not probe.get("ok"):
            browsers = Path(tool["folder"]) / "browsers"
            # Default CDN first, then a mirror reachable from mainland-China runners.
            for host in (None, "https://registry.npmmirror.com/-/binary/playwright"):
                extra = {"PLAYWRIGHT_BROWSERS_PATH": str(browsers)}
                if host:
                    extra["PLAYWRIGHT_DOWNLOAD_HOST"] = host
                try:
                    subprocess.run([node, str(Path(tool["module"]) / "cli.js"), "install", "chromium-headless-shell"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                                   env=child_env(extra))
                except Exception as error:
                    probe = {"ok": False, "reason": f"browser install failed: {type(error).__name__}"}
                    continue
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
                probe = _find_chromium(node, tool["module"])
                if probe.get("ok"):
                    break
        tool["ok"] = bool(probe.get("ok"))
        tool["executable"] = probe.get("executablePath") or ""
        tool["reason"] = probe.get("reason", "")
        if tool["executable"]:
            os.environ["EVOGIT_CHROMIUM_PATH"] = tool["executable"]
    _BROWSER_STATE["tool"] = tool
    if not tool["ok"]:
        _BROWSER_STATE["unavailable"] = tool["reason"] or "no launchable chromium"
    return tool


def _find_chromium(node, module) -> dict:
    try:
        result = subprocess.run([node, str(ROOT / "find_chromium.cjs"), module], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120, env=child_env())
        return json.loads(result.stdout.strip().splitlines()[-1])
    except Exception as error:
        return {"ok": False, "reason": f"chromium probe failed: {type(error).__name__}"}


def playwright_module(deadline: float | None = None) -> str | None:
    tool = ensure_playwright(deadline)
    return tool["module"] if tool.get("ok") else None


def browser_render_check(base_url: str, routes: list[str], deadline: float | None = None) -> dict:
    """Load routes in Chromium; report page errors and empty renders. Skips if no browser."""
    if _BROWSER_STATE.get("unavailable"):
        return {"status": "browser_unavailable", "reason": _BROWSER_STATE["unavailable"]}
    module = playwright_module(deadline)
    if not module:
        return {"status": "browser_unavailable", "reason": _BROWSER_STATE.get("unavailable", "playwright unavailable")}
    node = shutil.which("node") or "node"
    try:
        result = subprocess.run([node, str(ROOT / "smoke_runner.cjs"), base_url, module, json.dumps(routes)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                                env=child_env())
    except subprocess.TimeoutExpired:
        return {"status": "browser_timeout"}
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        report = {"status": "runner_error", "reason": (result.stderr or result.stdout)[-600:]}
    if report.get("status") == "browser_unavailable":
        _BROWSER_STATE["unavailable"] = report.get("reason", "chromium could not be launched")
    return report


def smoke(root: Path, deadline: float | None = None, routes: list[str] | None = None, browser: bool = True) -> dict:
    """Return {'ok': bool, 'error': str, ...}. `error` is written for a model repair prompt."""
    root = Path(root)
    report: dict = {"ok": False}
    with running_app(root) as app:
        if app.error:
            report.update(stage="startup", error=app.error + "\nSERVER OUTPUT:\n" + app.log_tail())
            return report
        state = app.host_state() or {}
        report["host"] = {k: state.get(k) for k in ("api_present", "api_loaded", "api_error", "init_error")}
        if state.get("api_present") and not state.get("api_loaded"):
            report.update(stage="api_load", error="backend/src/app.js failed to load in the host:\n" + str(state.get("api_error")))
            return report
        if state.get("init_error"):
            report.update(stage="api_init", error="backend/src/app.js init(ctx) failed:\n" + str(state.get("init_error")))
            return report
        status, ctype, body = _get(app.base_url + "/")
        html = body.decode("utf-8", "replace")
        if status != 200 or 'id="root"' not in html:
            report.update(stage="index", error=f"GET / returned {status}; expected the built frontend index.html")
            return report
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
        for src in scripts[:3]:
            if src.startswith("http"):
                continue
            s_status, s_type, _ = _get(app.base_url + src)
            if s_status != 200 or "javascript" not in s_type:
                report.update(stage="asset", error=f"GET {src} returned {s_status} ({s_type}); frontend assets are not served")
                return report
        d_status, _, d_body = _get(app.base_url + "/__evogit/deep/link")
        if d_status != 200 or b'id="root"' not in d_body:
            report.update(stage="spa_fallback", error=f"client-side route returned {d_status}; SPA fallback missing")
            return report
        if browser:
            render = browser_render_check(app.base_url, routes or ["/"], deadline)
            report["render"] = render
            if render.get("status") == "executed":
                bad = [r for r in render.get("routes", []) if r.get("page_errors") or r.get("empty")]
                if bad:
                    lines = []
                    for row in bad[:3]:
                        if row.get("empty"):
                            lines.append(f"route {row['route']}: page rendered no visible content")
                        for err in row.get("page_errors", [])[:3]:
                            lines.append(f"route {row['route']}: uncaught browser error: {err}")
                    runtime = state.get("runtime_errors") or []
                    report.update(stage="render", error="\n".join(lines) + ("\nSERVER ERRORS:\n" + "\n".join(runtime[-3:]) if runtime else ""))
                    return report
        report["ok"] = True
        return report
