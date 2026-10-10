"""929.V1 regression tests: shared contracts in every prompt, helper roles, repair acceptance,
rejected-block recovery, stream glitches, route focus, gap notes."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))

import model_transport  # noqa: E402
import requirement_context as rc  # noqa: E402

DOC = {
    "id": "ROOT", "name": "Workspace", "type": "FOLDER", "description": "Root rule: every quoted UI name is exact. ![image](x.png)",
    "children": [
        {"id": "REQ-1", "name": "Module A", "type": "FOLDER",
         "description": ("Module A intro. " + "x" * 700 + " Grid cells use the gridcell role named by their coordinate (for example, A1)."),
         "children": [{"id": "REQ-1-1", "name": "Open", "type": "ATOMIC", "description": "Open `Report A`.",
                       "scenarios": [{"name": "s", "steps": [{"keyword": "GIVEN", "content": "seed `Report A` and `alice`"}]}]}]},
        {"id": "REQ-2", "name": "Module B", "type": "FOLDER", "description": "Module B rule.",
         "children": [{"id": "REQ-2-1", "name": "Edit", "type": "ATOMIC", "description": "Edit cell in `Report A`.",
                       "scenarios": [{"name": "s", "steps": [{"keyword": "WHEN", "content": "type `42` into `A1`"}]}]}]},
    ]}


class ContractTests(unittest.TestCase):
    def test_global_contracts_include_every_module_rule_in_full(self):
        text = rc.global_contracts(DOC)
        self.assertIn("Root rule", text)
        self.assertNotIn("![image]", text)
        self.assertIn("named by their coordinate (for example, A1)", text, "V8 cut parent context at 600 chars")
        self.assertIn("Module B rule", text)

    def test_seed_index_counts_shared_values(self):
        text = rc.seed_index(DOC)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("`Report A` used by 2"), first)
        self.assertIn("`alice` used by 1", text)

    def test_spec_prompt_carries_contracts_of_other_modules(self):
        import acceptance
        from generated_application import work_units
        unit = [u for u in work_units(DOC, limit=10) if u["id"] == "REQ-2-1"][0]
        prompt = acceptance.spec_prompt(unit, "{}", "", "", rc.global_contracts(DOC), rc.seed_index(DOC))
        self.assertIn("gridcell role named by their coordinate", prompt, "unit in module B must see module A's grid contract")
        self.assertLess(prompt.index("GLOBAL CONTRACTS"), prompt.index("REQUIREMENTS TO TEST"))
        self.assertNotIn("PARENT CONTEXT", prompt, "ancestors already in the global contracts are not repeated")

    def test_unit_view_references_contract_ancestors(self):
        from generated_application import unit_view, work_units
        unit = [u for u in work_units(DOC, limit=10) if u["id"] == "REQ-2-1"][0]
        view = json.loads(unit_view(unit, rc.global_contracts(DOC)))
        self.assertTrue(all(a["description"] == "(see GLOBAL CONTRACTS)" for a in view["ancestors"]))
        self.assertEqual(view["requirement"]["id"], "REQ-2-1")


class HelperTests(unittest.TestCase):
    def test_control_resolves_named_grid_cells(self):
        text = (PACKAGE / "acceptance_assets/evogit-helpers.ts").read_text()
        self.assertIn("'gridcell'", text.split("NAMED_ITEMS")[1][:200])
        self.assertIn("NAMED_ITEMS.map", text)
        self.assertIn("export function cell(", text)


class RepairRuleTests(unittest.TestCase):
    def test_keep_repair(self):
        from tdd_loop import keep_repair
        self.assertTrue(keep_repair(1, 0))
        self.assertTrue(keep_repair(3, 1), "V8 reverted +3/-1")
        self.assertTrue(keep_repair(6, 2))
        self.assertTrue(keep_repair(4, 2), "930.V1 reverted +4/-2 twice in a row")
        self.assertFalse(keep_repair(3, 2))
        self.assertFalse(keep_repair(2, 1))
        self.assertFalse(keep_repair(1, 1))
        self.assertFalse(keep_repair(0, 0))


class RejectedBlockTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "frontend/src/pages").mkdir(parents=True)
        (self.root / "frontend/src/pages/Home.tsx").write_text("export default function Home() { return <main>Home</main>; }\n")
        (self.root / "frontend/src/App.tsx").write_text("export default function App() { return null; }\n")

    def test_unseen_file_replacement_attaches_file_to_retry(self):
        from source_blocks import RejectedBlocks, block_parser
        parse = block_parser(self.root, {"frontend/src/App.tsx"})
        reply = "<<<FILE frontend/src/pages/Home.tsx>>>\nexport default function Home() { return <main>New</main>; }\n<<<END FILE>>>"
        with self.assertRaises(RejectedBlocks) as caught:
            parse(reply, "stop")
        self.assertIn("return <main>Home</main>", caught.exception.correction)
        # The retry may now replace the file it has been shown.
        staged = parse(reply, "stop")
        self.assertIn("frontend/src/pages/Home.tsx", staged["staged"])

    def test_partial_application_reports_rejected_blocks(self):
        from source_blocks import block_parser
        parse = block_parser(self.root, {"frontend/src/App.tsx", "frontend/src/pages/Home.tsx"})
        reply = ("<<<EDIT frontend/src/App.tsx>>>\n<<<OLD>>>\nreturn null;\n<<<NEW>>>\nreturn <p/>;\n<<<END EDIT>>>\n"
                 "<<<EDIT frontend/src/pages/Home.tsx>>>\n<<<OLD>>>\nnot there\n<<<NEW>>>\nnew text\n<<<END EDIT>>>")
        staged = parse(reply, "stop")
        self.assertIn("frontend/src/App.tsx", staged["staged"])
        self.assertEqual(staged["rejected"][0]["path"], "frontend/src/pages/Home.tsx")
        self.assertEqual(staged["rejected"][0]["new"], "new text")

    def test_request_json_appends_correction(self):
        from source_blocks import block_parser
        from test_928_cost_controls import Client, _Chunk, _U
        for key in model_transport.LEARNED:
            model_transport.LEARNED[key] = None if key == "max_tokens_cap" else False
        bad = "<<<FILE frontend/src/pages/Home.tsx>>>\nexport default 1;\n<<<END FILE>>>"
        client = Client([iter([_Chunk(bad), _Chunk(finish="stop"), _Chunk(usage=_U())]),
                         iter([_Chunk(bad), _Chunk(finish="stop"), _Chunk(usage=_U())])])
        value = model_transport.request_json(client, "m", "s", "PROMPT", lambda d: d, [], [], lambda: None, time.monotonic() + 60,
                                             sleep=lambda s: None, parser=block_parser(self.root, set()))
        self.assertIn("frontend/src/pages/Home.tsx", value["staged"])
        retry_prompt = client.calls[1]["messages"][-1]["content"]
        self.assertTrue(retry_prompt.startswith("PROMPT"), "retry keeps the cached prefix")
        self.assertIn("return <main>Home</main>", retry_prompt)


class StreamGlitchTests(unittest.TestCase):
    def setUp(self):
        for key in model_transport.LEARNED:
            model_transport.LEARNED[key] = None if key == "max_tokens_cap" else False

    def _broken(self, text):
        def gen():
            from test_928_cost_controls import _Chunk
            yield _Chunk(text)
            raise json.JSONDecodeError("Unterminated string starting at", "{", 0)
        return gen()

    def test_short_broken_stream_is_retried_without_blaming_the_model(self):
        from test_928_cost_controls import Client, _Chunk, _U
        client = Client([self._broken("{\"ok\""), iter([_Chunk('{"ok": 1}'), _Chunk(finish="stop"), _Chunk(usage=_U())])])
        events = []
        value = model_transport.request_json(client, "m", "s", "P", lambda d: d, events, [], lambda: None, time.monotonic() + 60,
                                             sleep=lambda s: None)
        self.assertEqual(value, {"ok": 1})
        self.assertEqual(client.calls[1]["messages"][-1]["content"], "P", "no 'your answer was rejected' correction")
        self.assertTrue(any(e.get("reason") == "StreamGlitch" for e in events))

    def test_long_broken_stream_is_salvaged_as_truncated(self):
        from source_blocks import block_parser
        from test_928_cost_controls import Client
        root = Path(tempfile.mkdtemp())
        partial = "<<<FILE frontend/src/A.tsx>>>\n" + "// a\n" * 600 + "<<<END FILE>>>\n<<<FILE frontend/src/B.tsx>>>\nexport const b"
        client = Client([self._broken(partial)])
        value = model_transport.request_json(client, "m", "s", "P", lambda d: d, [], [], lambda: None, time.monotonic() + 60,
                                             sleep=lambda s: None, parser=block_parser(root, set()), attempts=1)
        self.assertIn("frontend/src/A.tsx", value["staged"])
        self.assertTrue(value["truncated"])


class RouteFocusTests(unittest.TestCase):
    def test_failure_url_maps_to_page_file(self):
        import route_map
        root = Path(tempfile.mkdtemp())
        (root / "frontend/src/pages").mkdir(parents=True)
        (root / "frontend/src/pages/ItemPage.tsx").write_text("export default function ItemPage() { return null; }\n")
        (root / "frontend/src/pages/ListPage.tsx").write_text("export function ListPage() { return null; }\n")
        (root / "frontend/src/App.tsx").write_text(
            "import ItemPage from './pages/ItemPage';\nimport { ListPage } from './pages/ListPage';\n"
            "export default function App() { return (<Routes>\n<Route path=\"/items\" element={<ListPage />} />\n"
            "<Route path=\"/items/:id\" element={<ItemPage />} />\n</Routes>); }\n")
        self.assertEqual(route_map.files_for_urls(root, ["http://127.0.0.1:4000/items/42"]), ["frontend/src/pages/ItemPage.tsx"])
        self.assertEqual(route_map.files_for_urls(root, ["http://127.0.0.1:4000/items"]), ["frontend/src/pages/ListPage.tsx"])

    def test_known_gaps_lists_stub_routes_and_duplicate_schemes(self):
        from unit_adaptation import known_gaps, large_files_note
        root = Path(tempfile.mkdtemp())
        (root / "frontend/src/pages").mkdir(parents=True)
        (root / "backend/src").mkdir(parents=True)
        (root / "frontend/src/pages/PlaceholderPage.tsx").write_text("export default function PlaceholderPage() { return <p>Coming soon</p>; }\n")
        (root / "frontend/src/pages/Detail.tsx").write_text("export default function Detail() { return null; }\n")
        (root / "frontend/src/App.tsx").write_text(
            "import PlaceholderPage from './pages/PlaceholderPage';\nimport Detail from './pages/Detail';\n"
            "const r = (<Routes><Route path=\"/a/:x/:y/issues\" element={<Detail />} /><Route path=\"/a/:id/issues\" element={<Detail />} />"
            "<Route path=\"/members\" element={<PlaceholderPage />} /><Route path=\"*\" element={<PlaceholderPage />} /></Routes>);\n")
        (root / "backend/src/app.js").write_text("//" + "x" * 40000)
        gaps = known_gaps(root)
        self.assertIn("/members", gaps)
        self.assertNotIn(" *", gaps)
        self.assertIn("/a/:x/:y/issues", gaps)
        self.assertIn("backend/src/app.js", large_files_note(root))


class PromptOrderTests(unittest.TestCase):
    def test_static_head_is_identical_across_units_and_source_notes_follow_source(self):
        from generated_application import source_section, static_prompt_head
        head1 = static_prompt_head("source_generation", "C", "S", "PLAN", "OUT", "SH")
        head2 = static_prompt_head("source_generation", "C", "S", "PLAN", "OUT", "SH")
        self.assertEqual(head1, head2)
        self.assertLess(head1.index("GLOBAL CONTRACTS"), head1.index("ARCHITECTURE PLAN"))
        root = Path(tempfile.mkdtemp())
        (root / "frontend/src").mkdir(parents=True)
        (root / "frontend/src/App.tsx").write_text("<Route path=\"/x\" element={<X />} />\n")
        from generated_application import source_context
        section = source_section(root, source_context(root))
        self.assertLess(section.index("CURRENT SOURCE"), section.index("CURRENT IMPLEMENTATION SURFACE"))

    def test_quality_profile_reasons_on_code(self):
        import budget_policy
        self.assertEqual(budget_policy.PROFILE, "quality")
        self.assertEqual(budget_policy.reasoning_for("source_generation"), "high")
        self.assertEqual(budget_policy.reasoning_for("test_repair_unit"), "high")
        self.assertEqual(budget_policy.reasoning_for("test_generation"), "low")


if __name__ == "__main__":
    unittest.main()


class HostResetTests(unittest.TestCase):
    def test_reset_endpoint_restores_seed_and_reloads_modules(self):
        import shutil
        import subprocess
        import urllib.request
        from app_runtime import running_app
        if not shutil.which("node"):
            self.skipTest("node missing")
        root = Path(tempfile.mkdtemp())
        (root / "backend/src").mkdir(parents=True)
        shutil.copy(PACKAGE / "template/backend/src/index.js", root / "backend/src/index.js")
        (root / "backend/src/app.js").write_text(
            "let calls = 0;\n"
            "module.exports = {\n"
            "  init(ctx) { if (!ctx.loadJson('c.json', null)) ctx.saveJson('c.json', { n: 0 }); },\n"
            "  async handle(req, res, ctx) {\n"
            "    if (ctx.match('POST', '/api/inc')) { const c = ctx.loadJson('c.json', { n: 0 }); c.n += 1; calls += 1; ctx.saveJson('c.json', c); return ctx.sendJson(res, 200, c); }\n"
            "    if (ctx.match('GET', '/api/c')) return ctx.sendJson(res, 200, { ...ctx.loadJson('c.json', {}), calls });\n"
            "    return false; } };\n")

        def call(base, method, path):
            request = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None)
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")

        with running_app(root, extra_env={"EVOGIT_TEST_RESET": "1"}) as app:
            self.assertIsNone(app.error)
            call(app.base_url, "POST", "/api/inc")
            status, value = call(app.base_url, "POST", "/api/inc")
            self.assertEqual(value["n"], 2)
            status, value = call(app.base_url, "POST", "/api/__evogit_reset")
            self.assertEqual(status, 200)
            status, value = call(app.base_url, "GET", "/api/c")
            self.assertEqual((value["n"], value["calls"]), (0, 0), "data and module state are back to the seed")
        with running_app(root) as app:
            try:
                status, _ = call(app.base_url, "POST", "/api/__evogit_reset")
            except urllib.error.HTTPError as error:
                status = error.code
            self.assertEqual(status, 404, "no reset endpoint without EVOGIT_TEST_RESET")


class StreamCutTests(unittest.TestCase):
    def setUp(self):
        for key in model_transport.LEARNED:
            model_transport.LEARNED[key] = None if key == "max_tokens_cap" else False

    def test_stream_without_finish_reason_is_treated_as_cut(self):
        from source_blocks import block_parser
        from test_928_cost_controls import Client, _Chunk
        root = Path(tempfile.mkdtemp())
        text = "<<<FILE frontend/src/A.tsx>>>\nexport const a = 1;\n<<<END FILE>>>\n<<<FILE frontend/src/B.tsx>>>\nexport const b ="
        client = Client([iter([_Chunk(text)])])   # no finish chunk, no usage: the connection dropped
        value = model_transport.request_json(client, "m", "s", "P", lambda d: d, [], [], lambda: None, time.monotonic() + 60,
                                             sleep=lambda s: None, parser=block_parser(root, set()), attempts=1)
        self.assertIn("frontend/src/A.tsx", value["staged"])
        self.assertNotIn("frontend/src/B.tsx", value["staged"], "a cut last block must not be applied as complete")
        self.assertTrue(value["truncated"])

    def test_cut_mid_generation_lowers_effort_for_the_retry(self):
        from test_928_cost_controls import Client, _Chunk, _U
        now = [0.0]

        def clock():
            now[0] += 50.0      # every clock read advances time: the failed attempt lasts > 60 s
            return now[0]

        def broken():
            yield _Chunk(reasoning="x" * 10)
            raise json.JSONDecodeError("Unterminated string starting at", "{", 0)

        client = Client([broken(), iter([_Chunk('{"ok": 1}'), _Chunk(finish="stop"), _Chunk(usage=_U())])])
        value = model_transport.request_json(client, "m", "s", "P", lambda d: d, [], [], lambda: None, 10**9,
                                             sleep=lambda s: None, clock=clock, reasoning="high")
        self.assertEqual(value, {"ok": 1})
        self.assertEqual(client.calls[0]["reasoning_effort"], "high")
        self.assertEqual(client.calls[1]["reasoning_effort"], "low")

    def test_early_gateway_error_keeps_effort(self):
        from test_928_cost_controls import Client, _Err, _Chunk, _U
        client = Client([_Err(400, "http2: server sent GOAWAY and closed the connection"),
                         iter([_Chunk('{"ok": 1}'), _Chunk(finish="stop"), _Chunk(usage=_U())])])
        model_transport.request_json(client, "m", "s", "P", lambda d: d, [], [], lambda: None, time.monotonic() + 60,
                                     sleep=lambda s: None, reasoning="high")
        self.assertEqual(client.calls[1]["reasoning_effort"], "high")


class SeedConflictTests(unittest.TestCase):
    def test_seed_conflicts_reach_plan_text_and_spec_prompt(self):
        import acceptance
        from generated_application import plan_text, validate_architecture, work_units
        plan = validate_architecture({"architecture": "A" * 50, "seed_conflicts": ["record X holds content 1; REQ-2-1 enters content 2"]})
        self.assertIn("SEED CONFLICTS", plan_text(plan))
        self.assertEqual(validate_architecture({"architecture": "A" * 50, "seed_conflicts": "none"})["seed_conflicts"], "")
        unit = [u for u in work_units(DOC, limit=10) if u["id"] == "REQ-2-1"][0]
        prompt = acceptance.spec_prompt(unit, "{}", "", "", rc.global_contracts(DOC), rc.seed_index(DOC), plan["seed_conflicts"])
        self.assertIn("REQ-2-1 enters content 2", prompt)
        self.assertLess(prompt.index("SEED CONFLICTS"), prompt.index("REQUIREMENTS TO TEST"))
