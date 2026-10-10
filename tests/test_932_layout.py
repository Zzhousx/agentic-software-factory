"""932.V1: one cached prefix for every code-writing stage, whole source in stable order, product conventions."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))

import generated_application as ga  # noqa: E402


class SharedPrefixTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for rel, age in (("frontend/src/pages/Old.tsx", 900), ("backend/src/store.js", 600), ("frontend/src/App.tsx", 0)):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// " + rel + "\n" + "x" * 2000 + "\n")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        ga.SHARED.update(contracts="C", seeds="S", plan="PLAN", outline="O", shared="SH")

    def tearDown(self):
        ga.SHARED.update(contracts="", seeds="", plan="", outline="", shared="")

    def test_every_stage_shares_head_and_source_prefix(self):
        section = ga.source_section(self.root, ga.source_context(self.root))
        prompts = [ga.shared_head() + section + ga.task_header(stage) + "details" for stage in
                   ("source_generation", "build_repair", "test_repair", "unit_review")]
        prefix = os.path.commonprefix(prompts)
        self.assertIn("frontend/src/App.tsx", prefix, "the whole source is inside the shared prefix")
        self.assertTrue(prefix.endswith("TASK STAGE: "), "stages differ only after the source")
        for prompt in prompts:
            self.assertEqual(prompt.count("TASK STAGE:"), 1)

    def test_whole_source_is_included_in_age_order(self):
        rendered = ga.render_context(ga.source_context(self.root))
        self.assertLess(rendered.index("pages/Old.tsx"), rendered.index("backend/src/store.js"))
        self.assertLess(rendered.index("backend/src/store.js"), rendered.index("frontend/src/App.tsx"))
        self.assertGreaterEqual(ga.FULL_SOURCE_CHARS, 400000)

    def test_conventions_reach_every_prompt(self):
        plan = ga.validate_architecture({"architecture": "A" * 60, "conventions": "Product X: pages at /owner/name",
                                         "seed_conflicts": ""})
        text = ga.plan_text(plan)
        self.assertIn("PRODUCT CONVENTIONS", text)
        self.assertIn("/owner/name", text)
        self.assertNotIn("SEED CONFLICTS", text)
        self.assertIn("PRODUCT CONVENTIONS", ga.CONTRACT)


class ReviewPolicyTests(unittest.TestCase):
    def test_review_stage(self):
        import budget_policy
        import tdd_loop
        self.assertEqual(budget_policy.reasoning_for("unit_review"), "high")
        self.assertEqual(budget_policy.budget_bucket("unit_review"), "repair")
        self.assertEqual(tdd_loop.UNIT_REPAIR_ROUNDS, 1)
        self.assertTrue(hasattr(tdd_loop.AcceptanceLoop, "review_unit"))


if __name__ == "__main__":
    unittest.main()
