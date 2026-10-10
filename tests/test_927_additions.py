"""927.V1 regression tests: context selection, spec parsing/grouping, streaming transport. Offline."""
import sys
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "arcbench-agent-runtime" / "src"))

import context_select  # noqa: E402
import model_transport  # noqa: E402
from acceptance import group_failures, leaf_of, leaves, spec_parser, cross_cutting_text  # noqa: E402


class ContextSelectionTests(unittest.TestCase):
    def test_hub_files_always_visible_in_large_projects(self):
        snapshot = {f"frontend/src/pages/Page{i}.tsx": "export default function Page%d() { return null; }\n" % i + "x" * 9000
                    for i in range(30)}
        snapshot["frontend/src/App.tsx"] = "import A from './pages/Page1';\n<Route path=\"/repos\" />\n" + "y" * 30000
        snapshot["backend/src/app.js"] = "ctx.match('POST', '/api/repos')\nmodule.exports = { init, handle };\n" + "z" * 30000
        snapshot["frontend/src/pages/PullRequests.tsx"] = "export default function PullRequests() {}\n// pull request merge review\n" + "p" * 8000
        context = context_select.select(snapshot, limit=90000, unit_text="Merge a pull request after review")
        self.assertTrue(context["large_files_omitted"])
        self.assertIn("frontend/src/App.tsx", context["files"])
        self.assertIn("backend/src/app.js", context["files"])
        self.assertIn("frontend/src/pages/PullRequests.tsx", context["files"])
        omitted = [p for p in snapshot if p not in context["files"]]
        self.assertTrue(omitted)
        self.assertTrue(all(p in context["outlines"] for p in omitted))
        rendered = context_select.render(context)
        self.assertIn("OUTLINE OF FILES NOT SHOWN", rendered)

    def test_outline_lists_exports_routes_and_api(self):
        items = context_select.outline("export default function Repo() {}\n<Route path=\"/r/:id\" element={<X/>} />\n"
                                       "if (ctx.match('GET', '/api/repos/:id')) {}\nmodule.exports = { handle };\n")
        self.assertIn("export Repo", items)
        self.assertIn("route /r/:id", items)
        self.assertIn("api GET /api/repos/:id", items)


class AcceptanceParsingTests(unittest.TestCase):
    def test_spec_parser_accepts_id_named_files(self):
        parse = spec_parser(["REQ-1.2"])
        files = parse("<<<FILE REQ-1.2.spec.ts>>>\nimport { test } from '@playwright/test';\ntest('REQ-1.2: x', async () => {});\n<<<END FILE>>>", "stop")
        self.assertEqual(list(files), ["REQ-1.2.spec.ts"])
        with self.assertRaises(ValueError):
            parse("<<<FILE other.spec.ts>>>\ntest('x', async () => {});\n<<<END FILE>>>", "stop")

    def test_leaf_of_title_and_file(self):
        self.assertEqual(leaf_of("REQ-2.3.1: Delete", "x.spec.ts"), "REQ-2.3.1")
        self.assertEqual(leaf_of("Delete", "REQ-4.spec.ts"), "REQ-4")

    def test_failures_sharing_a_missing_control_are_grouped_first(self):
        missing = "Error: Not found: control /^\\s*Sign in\\s*$/i (no visible element; candidate counts 0,0)"
        result = {"tests": {
            "a::1": {"status": "failed", "error": missing},
            "b::2": {"status": "failed", "error": missing},
            "c::3": {"status": "failed", "error": "Expected substring: \"Saved\""},
            "d::4": {"status": "passed", "error": ""},
            "e::<load>": {"status": "load_error", "error": "SyntaxError"},
        }}
        groups = group_failures(result)
        self.assertEqual(sorted(groups[0]), ["a::1", "b::2"])
        self.assertNotIn("e::<load>", [k for g in groups for k in g])

    def test_cross_cutting_requirements_found(self):
        doc = {"id": "ROOT", "children": [
            {"id": "REQ-1", "name": "Sign in", "description": "User signs in with seeded account alice/secret123",
             "scenarios": [{"name": "Sign in", "steps": [{"keyword": "WHEN", "content": "fill Username"}]}]},
            {"id": "REQ-2", "name": "Create issue", "description": "Issue form", "scenarios": []}]}
        self.assertEqual([n["id"] for n in leaves(doc)], ["REQ-1", "REQ-2"])
        text = cross_cutting_text(doc)
        self.assertIn("alice/secret123", text)
        self.assertNotIn("Issue form", text)


class _Chunk:
    def __init__(self, content=None, finish=None, usage=None, reasoning=None):
        delta = type("D", (), {"content": content, "reasoning_content": reasoning})()
        self.choices = [type("C", (), {"delta": delta, "finish_reason": finish})()] if (content or finish or reasoning) else []
        self.usage = usage


class _U:
    def model_dump(self):
        return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


class _Err(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


class StreamingClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []

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


class StreamingTransportTests(unittest.TestCase):
    def setUp(self):
        model_transport.LEARNED.update(max_tokens_cap=None, no_temperature=False, no_stream=False, no_stream_options=False)

    def call(self, client):
        return model_transport.request_json(client, "m", "s", "P", lambda d: d, [], [], lambda: None,
                                            time.monotonic() + 60, sleep=lambda s: None)

    def test_streamed_reply_is_assembled(self):
        stream = [_Chunk(reasoning="thinking..."), _Chunk('{"a": '), _Chunk('1}'), _Chunk(finish="stop"), _Chunk(usage=_U())]
        client = StreamingClient([iter(stream)])
        self.assertEqual(self.call(client), {"a": 1})
        self.assertTrue(client.calls[0]["stream"])

    def test_goaway_400_is_transient_and_not_learned(self):
        goaway = _Err(400, 'Post "https://gw/v1/chat/completions": http2: server sent GOAWAY and closed the connection; LastStreamID=1999')
        client = StreamingClient([goaway, iter([_Chunk('{"ok": 1}'), _Chunk(finish="stop")])])
        self.assertEqual(self.call(client), {"ok": 1})
        self.assertFalse(model_transport.LEARNED["no_temperature"])
        self.assertFalse(model_transport.LEARNED["no_stream"])
        self.assertIsNone(model_transport.LEARNED["max_tokens_cap"])

    def test_gateway_without_streaming_falls_back(self):
        class Plain:
            def __init__(self):
                msg = type("M", (), {"content": '{"x": 2}'})()
                self.choices = [type("C", (), {"message": msg, "finish_reason": "stop"})()]
                self.usage = _U()
        client = StreamingClient([_Err(400, "stream is not supported"), Plain()])
        self.assertEqual(self.call(client), {"x": 2})
        self.assertTrue(model_transport.LEARNED["no_stream"])


if __name__ == "__main__":
    unittest.main()
