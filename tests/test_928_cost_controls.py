"""928.V1 regression tests: per-stage reasoning control, learning rejected controls, cache-friendly order."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))

import model_transport  # noqa: E402
import budget_policy  # noqa: E402


class _U:
    def __init__(self, reasoning=0):
        self.reasoning = reasoning

    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 10 + self.reasoning, "total_tokens": 20 + self.reasoning,
                "completion_tokens_details": {"reasoning_tokens": self.reasoning}}


class _Chunk:
    def __init__(self, content=None, finish=None, usage=None, reasoning=None):
        delta = type("D", (), {"content": content, "reasoning_content": reasoning})()
        self.choices = [type("C", (), {"delta": delta, "finish_reason": finish})()] if (content or finish or reasoning) else []
        self.usage = usage


class _Err(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


class Client:
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


def ok_stream(text='{"ok": 1}', reasoning=0):
    return iter([_Chunk(text), _Chunk(finish="stop"), _Chunk(usage=_U(reasoning))])


class ReasoningControlTests(unittest.TestCase):
    def setUp(self):
        for key in model_transport.LEARNED:
            model_transport.LEARNED[key] = None if key == "max_tokens_cap" else False
        os.environ.pop("EVOGIT_REASONING_OVERRIDE", None)

    def call(self, client, **kw):
        events = []
        value = model_transport.request_json(client, "m", "s", "P", lambda d: d, events, [], lambda: None,
                                             time.monotonic() + 60, sleep=lambda s: None, **kw)
        return value, events

    def test_off_disables_thinking_and_low_sets_effort(self):
        client = Client([ok_stream(), ok_stream()])
        self.call(client, reasoning="off")
        self.call(client, reasoning="low")
        self.assertEqual(client.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", client.calls[0])
        self.assertEqual(client.calls[1]["reasoning_effort"], "low")
        self.assertNotIn("temperature", client.calls[1], "thinking mode does not support temperature")

    def test_rejected_controls_are_learned_and_dropped(self):
        client = Client([_Err(400, "Unrecognized request argument supplied: thinking"), ok_stream(), ok_stream()])
        value, _ = self.call(client, reasoning="off")
        self.assertEqual(value, {"ok": 1})
        self.assertTrue(model_transport.LEARNED["no_thinking_param"])
        self.assertNotIn("extra_body", client.calls[1])
        self.call(client, reasoning="off")
        self.assertNotIn("extra_body", client.calls[2])

    def test_empty_answer_retries_with_lower_effort(self):
        client = Client([iter([_Chunk(reasoning="x" * 100), _Chunk(finish="stop"), _Chunk(usage=_U(58000))]), ok_stream()])
        value, events = self.call(client, reasoning="high")
        self.assertEqual(value, {"ok": 1})
        self.assertEqual(client.calls[0]["reasoning_effort"], "high")
        self.assertEqual(client.calls[1]["reasoning_effort"], "low")

    def test_runaway_reasoning_is_cut_and_lowered(self):
        runaway = iter([_Chunk(reasoning="y" * 5000) for _ in range(20)])
        client = Client([runaway, ok_stream()])
        value, events = self.call(client, reasoning="high", reasoning_cap=20000)
        self.assertEqual(value, {"ok": 1})
        self.assertTrue(any("runaway" in (e.get("error_detail") or "") for e in events))
        self.assertEqual(client.calls[1]["reasoning_effort"], "low")

    def test_policy_profiles(self):
        self.assertEqual(budget_policy.REASONING_PROFILES["balanced"]["test_generation"], "off")
        self.assertEqual(budget_policy.REASONING_PROFILES["balanced"]["source_generation"], "low")
        self.assertEqual(budget_policy.REASONING_PROFILES["quality"]["source_generation"], "high")


class CacheOrderTests(unittest.TestCase):
    def test_stable_files_are_rendered_before_recently_changed_ones(self):
        import generated_application as ga
        root = Path(tempfile.mkdtemp())
        for rel in ("frontend/src/App.tsx", "frontend/src/pages/Old.tsx", "backend/src/app.js"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("// " + rel + "\n")
        old = time.time() - 3600
        os.utime(root / "frontend/src/pages/Old.tsx", (old, old))
        os.utime(root / "backend/src/app.js", (old + 10, old + 10))
        rendered = ga.render_context(ga.source_context(root))
        self.assertLess(rendered.index("pages/Old.tsx"), rendered.index("backend/src/app.js"))
        self.assertLess(rendered.index("backend/src/app.js"), rendered.index("frontend/src/App.tsx"))


if __name__ == "__main__":
    unittest.main()
