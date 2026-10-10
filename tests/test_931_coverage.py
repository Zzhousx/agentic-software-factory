"""931.V1 regression tests: every scenario reaches the test writer, one test per scenario, audit stage wiring."""
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))

import acceptance  # noqa: E402

BOILER = ("The visitor starts at the application home page in a fresh unauthenticated browser session. "
          "The evaluation seed contains the seeded record `Alpha` and values `1` and `2`. ") * 3
LEAF = {"id": "REQ-9-1", "name": "Edit", "description": "Users edit things. Escape cancels an edit.",
        "scenarios": [{"name": f"REQ-9-1 -the requested workflow {i}", "steps": [
            {"keyword": "GIVEN", "content": BOILER},
            {"keyword": "WHEN", "content": f"The user does step {i}. " + BOILER},
            {"keyword": "THEN", "content": "The result is persisted. " + BOILER}]} for i in range(1, 8)]}


class ScenarioCoverageTests(unittest.TestCase):
    def test_every_scenario_is_visible_despite_boilerplate(self):
        text = acceptance.describe_leaf(LEAF)
        for i in range(1, 8):
            self.assertIn(f"Scenario {i}/7", text)
            self.assertIn(f"step {i}.", text)
        self.assertEqual(text.count("evaluation seed contains"), 1, "repeated sentences are shown once")
        self.assertLess(len(text), 2500)

    def test_spec_prompt_states_required_test_count(self):
        unit = {"id": "REQ-9-1", "ancestors": [], "requirement": LEAF}
        prompt = acceptance.spec_prompt(unit, "{}", "")
        self.assertIn("TESTS REQUIRED: REQ-9-1: 7", prompt)
        self.assertIn("Exactly ONE test per listed scenario", prompt)

    def test_parser_sends_back_a_short_answer_once(self):
        parse = acceptance.spec_parser(["REQ-9-1"], {"REQ-9-1": 3})
        one = "<<<FILE REQ-9-1.spec.ts>>>\nimport { test } from '@playwright/test';\ntest('REQ-9-1 #1: a', async () => {});\n<<<END FILE>>>"
        with self.assertRaises(ValueError) as caught:
            parse(one, "stop")
        self.assertIn("REQ-9-1: 1 of 3", str(caught.exception))
        self.assertIn("REQ-9-1.spec.ts", parse(one, "stop"), "the second short answer is accepted rather than lost")
        three = one.replace("<<<END FILE>>>", "test('REQ-9-1 #2: b', async () => {});\ntest(\"REQ-9-1 #3: c\", async () => {});\n<<<END FILE>>>")
        self.assertIn("REQ-9-1.spec.ts", acceptance.spec_parser(["REQ-9-1"], {"REQ-9-1": 3})(three, "stop"))


class AuditWiringTests(unittest.TestCase):
    def test_audit_stage_policy(self):
        import budget_policy
        self.assertEqual(budget_policy.reasoning_for("requirement_audit"), "high")
        self.assertEqual(budget_policy.budget_bucket("requirement_audit"), "repair")

    def test_review_prompt_lists_the_checks(self):
        import tdd_loop
        for phrase in ("exact accessible name", "exact message text", "persistence", "<<<NO CHANGES>>>", "failing tests"):
            self.assertIn(phrase, tdd_loop.REVIEW_INTRO)


if __name__ == "__main__":
    unittest.main()
