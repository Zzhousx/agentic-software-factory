"""926.V1 regression tests for the 925.V1 zero-pass causes. Offline: no model, no npm registry.

Run from the package root:  python -m unittest discover -s tests -v
(Node.js is required for the host/smoke tests; they are skipped when node is absent.)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "arcbench-agent-runtime" / "src"))

import model_transport  # noqa: E402
from model_transport import clean_text, extract_json, request_json  # noqa: E402
from source_blocks import block_parser, parse_blocks, stage_ops, write_staged  # noqa: E402

NODE = shutil.which("node")


class _Usage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


class _Resp:
    def __init__(self, content, finish="stop"):
        msg = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": msg, "finish_reason": finish})()]
        self.usage = _Usage()


class _HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


class FakeClient:
    """Scripted chat.completions client: each item is a response or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def with_options(self, **_):
        return self

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **params):
        self.calls.append(params)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def call(client, validator=lambda d: d, **kw):
    model_transport.LEARNED.update(max_tokens_cap=None, no_temperature=False, no_stream=True, no_stream_options=False, no_thinking_param=False, no_reasoning_effort=False)
    return request_json(client, "m", "stage", "PROMPT", validator, [], [], lambda: None,
                        time.monotonic() + 60, sleep=lambda s: None, **kw)


class TransportTests(unittest.TestCase):
    def test_think_block_and_prose_are_stripped(self):
        self.assertEqual(extract_json('<think>plan {not json}</think>\nSure:\n```json\n{"a": 1}\n```\nbye'), {"a": 1})
        self.assertEqual(clean_text("<think>x</think> hi"), "hi")

    def test_think_prefixed_selection_is_accepted(self):
        client = FakeClient([_Resp('<think>reasoning</think>{"asset_id": "generic-widgets"}')])
        self.assertEqual(call(client)["asset_id"], "generic-widgets")
        self.assertEqual(len(client.calls), 1)

    def test_truncation_retry_carries_shorten_instruction(self):
        client = FakeClient([_Resp('{"files": [{"path": "a", "content": "xx', "length"), _Resp('{"ok": true}')])
        self.assertEqual(call(client, truncation_hint="SHORTER PLEASE"), {"ok": True})
        self.assertIn("CUT OFF", client.calls[1]["messages"][-1]["content"])
        self.assertIn("SHORTER PLEASE", client.calls[1]["messages"][-1]["content"])

    def test_http_400_drops_temperature_then_reasoning_control_then_caps_max_tokens(self):
        client = FakeClient([_HTTPError(400), _HTTPError(400), _HTTPError(400), _Resp('{"ok": 1}')])
        self.assertEqual(call(client, max_tokens=32000, reasoning="off", attempts=4), {"ok": 1})
        self.assertIn("temperature", client.calls[0])
        self.assertNotIn("temperature", client.calls[1])
        self.assertIn("extra_body", client.calls[1])
        self.assertNotIn("extra_body", client.calls[2])
        self.assertEqual(client.calls[3]["max_tokens"], 8192)


class BlockProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "backend/src").mkdir(parents=True)
        (self.root / "frontend/src").mkdir(parents=True)
        (self.root / "backend/src/app.js").write_text("const a = 1;\nmodule.exports = {};\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_truncated_reply_keeps_complete_blocks(self):
        text = ("<<<FILE frontend/src/App.tsx>>>\nexport default function App() { return null; }\n<<<END FILE>>>\n"
                "<<<FILE frontend/src/Big.tsx>>>\nexport const cut = ")
        parsed = parse_blocks(text, "length")
        self.assertEqual([op["path"] for op in parsed["ops"]], ["frontend/src/App.tsx"])
        self.assertEqual(parsed["incomplete"], ["frontend/src/Big.tsx"])

    def test_edit_protected_and_outside_paths(self):
        text = ("<<<EDIT backend/src/app.js>>>\n<<<OLD>>>\nconst a = 1;\n<<<NEW>>>\nconst a = 2;\n<<<END EDIT>>>\n"
                "<<<FILE backend/src/index.js>>>\nserver\n<<<END FILE>>>\n<<<FILE package.json>>>\n{}\n<<<END FILE>>>\n")
        staged = stage_ops(self.root, parse_blocks(text, "stop"))
        self.assertEqual(sorted(staged["staged"]), ["backend/src/app.js"])
        self.assertEqual(len(staged["warnings"]), 2)
        write_staged(self.root, staged["staged"])
        self.assertIn("const a = 2;", (self.root / "backend/src/app.js").read_text())

    def test_unmatched_edit_is_reported_for_retry(self):
        parse = block_parser(self.root)
        with self.assertRaises(ValueError) as ctx:
            parse("<<<EDIT backend/src/app.js>>>\n<<<OLD>>>\nmissing\n<<<NEW>>>\nx\n<<<END EDIT>>>", "stop")
        self.assertIn("not found", str(ctx.exception))

    def test_no_changes_marker(self):
        self.assertTrue(block_parser(self.root)("<<<NO CHANGES>>>", "stop")["no_changes"])

    def test_json_fallback(self):
        parsed = parse_blocks('{"files": [{"path": "frontend/src/A.tsx", "content": "x"}]}', "stop")
        self.assertEqual(parsed["ops"][0]["path"], "frontend/src/A.tsx")


@unittest.skipUnless(NODE, "node is required")
class HostAndSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "project"
        shutil.copytree(PACKAGE / "template", self.root, ignore=shutil.ignore_patterns("node_modules", "dist", "template.yaml"))
        dist = self.root / "frontend/dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text('<!doctype html><div id="root"></div><script type="module" src="/assets/app.js"></script>')
        (dist / "assets/app.js").write_text("document.getElementById('root').textContent='ok';")

    def tearDown(self):
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def smoke(self):
        from app_runtime import smoke
        return smoke(self.root, browser=False)

    def test_template_host_passes_smoke(self):
        self.assertTrue(self.smoke()["ok"])

    def test_crashing_api_module_is_detected_but_ui_stays_up(self):
        (self.root / "backend/src/app.js").write_text("require('express');\nmodule.exports = { handle() {} };\n")
        report = self.smoke()
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "api_load")
        self.assertIn("express", report["error"])
        from app_runtime import running_app
        with running_app(self.root) as app:
            self.assertIsNone(app.error)
            with urllib.request.urlopen(app.base_url + "/deep/route") as response:
                self.assertIn(b'id="root"', response.read())

    def test_api_module_with_ctx_helpers_and_isolated_data(self):
        (self.root / "backend/src/app.js").write_text(
            "module.exports = { init(ctx) { if (!ctx.loadJson('items', null)) ctx.saveJson('items', [{ id: '1' }]); },\n"
            "  async handle(req, res, ctx) { let p; if ((p = ctx.match('GET', '/api/items/:id'))) return ctx.sendJson(res, 200, { id: p.id, all: ctx.loadJson('items', []) }); return false; } };\n")
        from app_runtime import running_app
        with running_app(self.root) as app:
            with urllib.request.urlopen(app.base_url + "/api/items/7") as response:
                self.assertEqual(json.loads(response.read()), {"id": "7", "all": [{"id": "1"}]})
        self.assertFalse((self.root / "backend/data").exists(), "local runs must not write the delivered data dir")

    def test_server_started_from_backend_cwd(self):
        # The platform runs `npm run start` in backend/: static paths must not depend on cwd.
        from app_runtime import running_app
        with running_app(self.root) as app:
            with urllib.request.urlopen(app.base_url + "/assets/app.js") as response:
                self.assertEqual(response.status, 200)


class EntrypointTests(unittest.TestCase):
    def test_main_never_exits_non_zero(self):
        out = Path(tempfile.mkdtemp())
        env = {**os.environ, "ARCBENCH_TASK_DIR": str(out / "missing"), "ARCBENCH_OUTPUT_DIR": str(out / "ws"),
               "PYTHONPATH": os.pathsep.join([str(PACKAGE), str(PACKAGE / "arcbench-agent-runtime" / "src")]),
               "PATH": os.environ.get("PATH", "")}
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL"):
            env.pop(key, None)
        result = subprocess.run([sys.executable, str(PACKAGE / "main.py")], env=env, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        failure = json.loads((out / "ws" / "AGENT_FAILURE.json").read_text())
        self.assertEqual(failure["status"], "degraded_delivery")
        self.assertTrue((out / "ws" / "backend/src/index.js").exists())
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
